"""攻略知识库：入库（增量）、混合检索（向量 + BM25，RRF 融合）、结构化事实（闭馆日）"""

import logging
from dataclasses import dataclass
from functools import cached_property
from typing import Literal

from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from app.knowledge.bm25 import BM25

logger = logging.getLogger(__name__)

SearchMode = Literal["vector", "keyword", "hybrid"]


@dataclass
class Hit:
    """一条检索结果"""

    chunk_id: str
    doc_id: str
    title: str
    section: str
    updated_at: str
    text: str
    score: float


def rrf(rankings: list[list[str]], k: int = 60) -> dict[str, float]:
    """RRF（Reciprocal Rank Fusion，倒数排名融合）：把多个排序结果合成一个

    每个结果的得分 = Σ 1 / (k + 它在每个排序里的名次)
    只看名次、不看原始分数：向量的相似度和 BM25 的分数根本不在一个尺度上，没法直接相加，名次可以
    k=60 是论文里的经验值，作用是让"第 1 名"和"第 2 名"的差距不要太悬殊
    """
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1 / (k + rank)
    return fused


class KnowledgeBase:
    def __init__(self, embeddings: Embeddings, persist_directory: str | None = None,
                 collection_name: str = "travel_guides", client=None):
        # persist_directory：存到磁盘（开发、生产）；不传并且传了 client：用测试提供的内存客户端
        self.vs = Chroma(
            collection_name=collection_name,
            embedding_function=embeddings,
            persist_directory=persist_directory,
            client=client,
            collection_metadata={"hnsw:space": "cosine"},  # 用余弦相似度（文本向量的常规选择）
        )
        self._corpus_cache: dict[str, list[Document]] = {}

    # ---------- 入库 ----------

    def ingest(self, documents: list[list[Document]]) -> dict[str, int]:
        """增量入库：内容没变的文档跳过；变了的先删掉旧的块，再写入新的块"""
        stats = {"added": 0, "skipped": 0, "removed": 0}
        for chunks in documents:
            if not chunks:
                continue
            doc_id, new_hash = chunks[0].metadata["doc_id"], chunks[0].metadata["content_hash"]
            old = self.vs.get(where={"doc_id": doc_id}, include=["metadatas"])
            if old["ids"] and old["metadatas"][0].get("content_hash") == new_hash:
                stats["skipped"] += 1
                continue
            if old["ids"]:
                # 文档改短了，旧的第 5 块在新版本里不存在了。不先删掉，就会留下过期的内容被检索出来
                self.vs.delete(ids=old["ids"])
                stats["removed"] += len(old["ids"])
            self.vs.add_documents(chunks, ids=[c.id for c in chunks])
            stats["added"] += len(chunks)
        self._corpus_cache.clear()  # 内容变了，BM25 用的语料缓存要清掉
        return stats

    def remove_missing(self, keep_doc_ids: set[str]) -> int:
        """源文件被删除的文档，也要从知识库里删掉"""
        everything = self.vs.get(include=["metadatas"])
        stale = [i for i, m in zip(everything["ids"], everything["metadatas"]) if m["doc_id"] not in keep_doc_ids]
        if stale:
            self.vs.delete(ids=stale)
            self._corpus_cache.clear()
        return len(stale)

    # ---------- 检索 ----------

    def corpus(self, city: str) -> list[Document]:
        """某个城市的全部块（BM25 需要在全部文本上算分）。数据量小，直接缓存在内存里"""
        if city not in self._corpus_cache:
            data = self.vs.get(where={"city": city}, include=["documents", "metadatas"])
            self._corpus_cache[city] = [
                Document(id=i, page_content=text, metadata=meta)
                for i, text, meta in zip(data["ids"], data["documents"], data["metadatas"])
            ]
        return self._corpus_cache[city]

    def search(self, query: str, city: str, k: int = 4, mode: SearchMode = "hybrid") -> list[Hit]:
        corpus = {doc.id: doc for doc in self.corpus(city)}
        if not corpus:
            return []
        candidates = max(k * 2, 8)  # 每一路多取一些，融合后再截断

        rankings: list[list[str]] = []
        if mode in ("vector", "hybrid"):
            # filter：只在这个城市的文档里找（元数据过滤），问北京的事不会搜出上海的攻略
            results = self.vs.similarity_search_with_score(query, k=candidates, filter={"city": city})
            rankings.append([doc.id for doc, _distance in results])
        if mode in ("keyword", "hybrid"):
            ids = list(corpus)
            scores = BM25([corpus[i].page_content for i in ids]).scores(query)
            ranked = sorted(zip(ids, scores), key=lambda pair: pair[1], reverse=True)
            rankings.append([i for i, score in ranked[:candidates] if score > 0])

        fused = rrf(rankings)
        top = sorted(fused.items(), key=lambda pair: pair[1], reverse=True)[:k]
        hits = []
        for chunk_id, score in top:
            doc = corpus[chunk_id]
            m = doc.metadata
            hits.append(Hit(chunk_id, m["doc_id"], m["title"], m["section"], m["updated_at"], doc.page_content, score))
        return hits

    # ---------- 结构化事实 ----------

    def closures(self, city: str) -> dict[str, set[int]]:
        """景点 → 闭馆的星期几（1=周一）。来自文档的元数据，不需要模型参与"""
        result: dict[str, set[int]] = {}
        for doc in self.corpus(city):
            name, days = doc.metadata.get("attraction"), doc.metadata.get("closed_weekdays")
            if name and days:
                result[name] = {int(d) for d in str(days).split(",") if d.strip()}
        return result


def format_hits(hits: list[Hit]) -> str:
    """给模型看的检索结果：编号 + 出处 + 内容。编号让模型可以在回答里标注 [1] [2]"""
    if not hits:
        return "知识库里没有找到相关内容。"
    blocks = []
    for n, hit in enumerate(hits, start=1):
        blocks.append(f"[{n}] 《{hit.title}》{hit.section}（更新于 {hit.updated_at}）\n{hit.text}")
    return "\n\n".join(blocks)