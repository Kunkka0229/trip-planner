"""用户的长期记忆：召回（读）和抽取（写）

一条记忆 = store 里的一个 item：
  namespace = ("users", 用户 ID, "memories")
  key       = 随机 ID
  value     = {"text": "带一岁宝宝出行，需要午休", "category": "party", "source": thread_id}
"""

import asyncio
import json
import logging
import uuid
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.store.base import BaseStore, SearchItem
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

MAX_MEMORIES = 30  # 每个用户最多记多少条，太多了召回时噪音大，也占 prompt

def namespace(user_id: str) -> tuple[str, ...]:
    return ("users", user_id, "memories")

# ---------- 读：召回 ----------
async def recall(store: BaseStore | None, user_id: str | None, query: str, limit: int = 8) -> list[str]:
    """召回和 query 最相关的记忆。有向量索引时按语义相似度排序，没有时 query 不起作用，按存储顺序返回前 limit 条"""
    if store is None or not user_id:
        return []
    items = await store.asearch(namespace(user_id), query=query, limit=limit)
    return [item.value["text"] for item in items]

def format_memories(memories: list[str]) -> str:
    """拼进 prompt 的文字。说清楚优先级：本次需求 > 长期偏好"""
    if not memories:
        return ""
    lines = "\n".join(f"- {m}" for m in memories)
    return f"\n\n【用户的长期偏好】（来自以往的对话；和本次需求冲突时，以本次需求为准）\n{lines}"

# ---------- 写：抽取 ----------
EXTRACT_SYSTEM_PROMPT = """你负责维护旅行助手对用户的长期记忆。你会看到"已有的记忆"和"用户刚说的话"，
判断有没有值得长期记住的信息，输出要做的修改。

值得记住的：会在以后的旅行中反复用到的稳定偏好和事实。
  例如：同行人员情况（带一岁宝宝、父母年纪大）、饮食（不吃辣、素食）、节奏（不喜欢早起）、住宿（只住连锁酒店）、兴趣（喜欢小众博物馆）
不要记住的：
  - 只和这一次旅行有关的安排（"这次想去故宫"、"第二天换个餐厅"）
  - 敏感信息：身份证号、手机号、详细住址、银行卡、具体的病情

规则：
- 新信息和已有记忆说的是同一件事、但内容变了 → update（例如以前"不吃辣"，现在说"可以吃一点辣了"）
- 用户明确否定了某条记忆 → delete
- 每条记忆一句话，以用户为主语，例如"带一岁宝宝出行，中午需要回酒店午休"
- 没有要改的就输出空列表

只输出一个 JSON 对象：
{"add": [{"text": "...", "category": "party|diet|pace|lodging|interest|budget|other"}],
 "update": [{"key": "已有记忆的 key", "text": "新的内容"}],
 "delete": ["已有记忆的 key"]}"""
 
class NewMemory(BaseModel):
    text: str = Field(min_length=2, max_length=100)
    category: Literal["party", "diet", "pace", "lodging", "interest", "budget", "other"] = "other"

class UpdatedMemory(BaseModel):
    key: str
    text: str = Field(min_length=2, max_length=100)
    
class MemoryOps(BaseModel):
    add: list[NewMemory] = []
    update: list[UpdatedMemory] = []
    delete: list[str] = []
    
async def list_memories(store: BaseStore, user_id: str) -> list[SearchItem]:
    # 不带 query 的 search：返回这个 namespace 下的 item（最多 MAX_MEMORIES 条）
    return await store.asearch(namespace(user_id), limit=MAX_MEMORIES)

async def update_memories(store: BaseStore, llm: BaseChatModel, user_id: str, user_texts: list[str], source: str) -> MemoryOps:
    """让模型对比"已有记忆"和"新的话"，输出增删改操作，再由代码执行

    为什么不直接把新的话存下来？
      - 大部分话不值得记（"第二天换个餐厅"）
      - 新旧信息会冲突，只追加不更新，记忆里就会同时有"不吃辣"和"爱吃辣"
    """
    texts = [t.strip() for t in user_texts if t and t.strip()]
    if not texts:
        return MemoryOps()
    existing = await list_memories(store, user_id)
    existing_view = [{"key": item.key, "text": item.value["text"]} for item in existing]
    prompt = (
        f"【已有的记忆】\n{json.dumps(existing_view, ensure_ascii=False)}\n\n"
        f"【用户刚说的话】\n" + "\n".join(f"- {t}" for t in texts)
    )
    response = await llm.ainvoke([SystemMessage(EXTRACT_SYSTEM_PROMPT), HumanMessage(prompt)], temperature=0)
    text = response.text
    ops = MemoryOps.model_validate_json(text[text.find("{") : text.rfind("}") + 1])

    # 模型给出的 key 必须真实存在：不能让它凭空"更新"或"删除"一条不存在的记忆
    known = {item.key: item for item in existing}
    ns = namespace(user_id)
    for item in ops.update:
        if item.key in known:
            category = known[item.key].value.get("category", "other")  # 更新内容，保留原来的分类
            await store.aput(ns, item.key, {"text": item.text, "category": category, "source": source})
    
    for key in ops.delete:
        if key in known:
            await store.adelete(ns, key)

    room = MAX_MEMORIES - len(known) + len([k for k in ops.delete if k in known])
    for memory in ops.add[: max(room, 0)]:
        await store.aput(ns, uuid.uuid4().hex, {"text": memory.text, "category": memory.category, "source": source})
    
    logger.info("记忆更新: +%d ~%d -%d", len(ops.add), len(ops.update), len(ops.delete))
    return ops

class MemoryWriter:
    """在后台更新记忆，不拖慢用户的请求

    热路径 vs 后台：
      热路径：回复用户之前先更新记忆 → 每次对话多等一次模型调用
      后台：  先回复用户，再慢慢整理记忆 → 用户无感；代价是"刚说的话"要到下一轮才生效
    记忆不需要实时生效，所以选后台
    """
    def __init__(self, llm: BaseChatModel | None) -> None:
        # 记忆抽取用自己的模型，不和规划共用：
        #   1. 可以换成更便宜的小模型（抽取偏好是个简单任务，P12 讲模型路由）
        #   2. 后台任务不会和主流程抢同一个模型对象（测试里的假模型就是被抢了才发现这个问题）
        # llm 为 None（没配置大模型）时，什么都不做
        self.llm = llm
        # 必须保存任务的引用！asyncio 只对任务保持"弱引用"，没人引用的任务可能在执行到一半时被垃圾回收
        # （Python 官方文档 asyncio.create_task 的 Important 提示里专门讲了这个坑）
        self._tasks: set[asyncio.Task] = set()
    
    def schedule(self, store: BaseStore | None, user_id: str, texts: list[str], source: str) -> None:
        if store is None or self.llm is None:
            return
        llm = self.llm
        
        async def run() -> None:
            try:
                await update_memories(store, llm, user_id, texts, source)
            except Exception:  # 记忆是锦上添花的功能，失败了记日志就行，不能影响主流程
                logger.exception("更新记忆失败")
        
        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)  # 任务结束后自动从集合里移除
        
    async def drain(self) -> None:
        """等所有后台任务完成：服务关闭前调用，测试里也用它"""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)


