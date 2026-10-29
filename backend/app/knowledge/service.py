"""知识库单例：按配置创建，没有配置 embedding 模型时返回 None（相关功能自动关闭）"""

from functools import lru_cache

from app.config import BACKEND_DIR, get_settings
from app.knowledge.base import KnowledgeBase
from app.memory.store import index_config

@lru_cache
def get_knowledge_base() -> KnowledgeBase | None:
    index = index_config()  # 复用 P7 的 embedding 配置
    if index is None:
        return None
    return KnowledgeBase(index["embed"], persist_directory=str(BACKEND_DIR / get_settings().kb_dir))