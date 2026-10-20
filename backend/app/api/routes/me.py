"""P7：用户查看和删除自己的长期记忆

《个人信息保护法》要求用户能查阅、删除自己的个人信息。"AI 记住了我什么"必须对用户透明、可控
"""
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from langgraph.store.base import BaseStore
from pydantic import BaseModel

from app.memory.memories import list_memories, namespace

router = APIRouter(prefix="/me", tags=["我的"])

UserId = Annotated[str, Header(alias="X-User-Id", max_length=64)]


def get_store(http: Request) -> BaseStore:
    """启动时打开的 store 挂在 app.state 上（见 main.py 的 lifespan）

    app.state 是动态容器，直接写 http.app.state.store 时编辑器只知道它是 Any，
    没有补全、也不能跳转到 aget / adelete 的定义；经过这个函数后类型就是 BaseStore 了（和 trip.py 的 get_graph 一样）
    """
    return http.app.state.store


Store = Annotated[BaseStore, Depends(get_store)]


class MemoryItem(BaseModel):
    key: str
    text: str
    category: str
    updated_at: str

@router.get("/memories", response_model=list[MemoryItem], summary="我的长期记忆")
async def get_memories(store: Store, user_id: UserId = "anonymous"):
    items = await list_memories(store, user_id)
    return [
        MemoryItem(
            key=i.key,
            text=i.value["text"],
            category=i.value.get("category", "other"),
            updated_at=i.updated_at.isoformat()
        )
        for i in items
    ]

@router.delete("/memories/{key}", summary="删除一条记忆")
async def delete_memory(key: str, store: Store, user_id: UserId = "anonymous"):
    # namespace 里带着 user_id：只能删自己的，别人的 key 在自己的 namespace 里根本找不到
    if await store.aget(namespace(user_id), key) is None:
        raise HTTPException(status_code=404, detail="记忆不存在")
    await store.adelete(namespace(user_id), key)
    return {"success": True}

@router.delete("/memories", summary="清空我的全部记忆")
async def clear_memories(store: Store, user_id: UserId = "anonymous"):
    items = await list_memories(store, user_id)
    for item in items:
        await store.adelete(namespace(user_id), item.key)
    return {"success": True, "deleted": len(items)}
