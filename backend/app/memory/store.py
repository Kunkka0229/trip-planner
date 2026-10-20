"""长期记忆的存储：LangGraph Store

checkpointer（P5）vs store（P7）：
  checkpointer：按 thread 存，记的是"这一次会话"的完整状态，会话之间互不相通 → 短期记忆
  store：       按 namespace 存，任意会话都能读写，比如 ("users", "u1", "memories") → 长期记忆

STORE_URL 的写法和 CHECKPOINT_URL 一样：memory / sqlite:///... / postgresql://...
配置了 EMBEDDING_MODEL 时，会给记忆建向量索引，召回时按语义相似度排序；没配置时不排序，按存储顺序返回
"""

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from typing import Any

from langchain_openai import OpenAIEmbeddings
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

from app.config import BACKEND_DIR, get_settings

def index_config() -> dict[str, Any] | None:
    """向量索引配置。fields=["text"]：只对记忆的 text 字段做 embedding"""
    
    settings = get_settings()
    if not settings.embedding_model:
        return None
    embeddings = OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        # OpenAI 兼容接口（百炼等）不支持 OpenAI 专用的"按 token 切分超长文本"逻辑，要关掉，否则会报错
        check_embedding_ctx_length=False,
    )
    return {"embed": embeddings, "dims": settings.embedding_dims, "fields": ["text"]}

@asynccontextmanager
async def open_store(url: str) -> AsyncGenerator[BaseStore, None]:
    index = index_config()
    if url == "memory":
        yield InMemoryStore(index=index)
    elif url.startswith("sqlite:///"):
        from langgraph.store.sqlite.aio import AsyncSqliteStore
        
        path = BACKEND_DIR / url.removeprefix("sqlite:///")
        path.parent.mkdir(parents=True, exist_ok=True)
        
        async with AsyncSqliteStore.from_conn_string(str(path), index=index) as store:
            await store.setup()  # 建表（向量索引用的是 sqlite-vec 扩展，安装 langgraph-checkpoint-sqlite 时已经带上了）
            yield store
    elif url.startswith(("postgresql://", "postgres://")):
        from langgraph.store.postgres.aio import AsyncPostgresStore  # 需要 pgvector 扩展才能用向量索引
        
        async with AsyncPostgresStore.from_conn_string(url, index=index) as store:
            await store.setup()
            yield store
    else:
        raise ValueError(f"不支持的 STORE_URL: {url}")