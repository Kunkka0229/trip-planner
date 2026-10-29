"""读取知识库文档并切分成块（chunk）

文档格式：Markdown + 开头的元数据（front matter）
  ---
  doc_id: beijing-gugong
  city: 北京
  closed_weekdays: 1          ← 结构化事实：周一闭馆（1=周一 … 7=周日，多个用逗号隔开）
  ---
  ## 开放时间与闭馆
  正文……

切分策略（和你 rag/ 项目里只用 RecursiveCharacterTextSplitter 的区别）：
  1. 先按 "## 标题" 切成小节：一个小节讲一件事，检索出来的内容是完整的、有上下文的
  2. 小节太长再按字数切（RecursiveCharacterTextSplitter）
  3. 每个块都带上元数据：属于哪篇文档、哪个城市、哪个小节，检索时可以按城市过滤，回答时可以标出处
"""

import hashlib
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter

DOCS_DIR = Path(__file__).resolve().parent.parent.parent / "knowledge" / "docs"

def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """拆出开头 --- 之间的 key: value，返回 (元数据, 正文)"""
    
    if not text.startswith("---"):
        return {}, text
    
    header, _, body = text[3:].partition("\n---")
    meta = {}
    for line in header.strip().splitlines():
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    return meta, body

header_splitter = MarkdownHeaderTextSplitter(headers_to_split_on=[("##", "section")])
# 中文的切分符：先按段落，再按句号、问号……最后才按字切
size_splitter = RecursiveCharacterTextSplitter(
    chunk_size=300, chunk_overlap=50, separators=["\n\n", "\n", "。", "！", "？", "；", "，", ""]
)

def load_document(path: Path) -> list[Document]:
    """一篇文档 → 若干个块。块的 id 是 "doc_id#序号"，重复入库时会覆盖而不是重复添加"""
    
    text = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(text)
    doc_id = meta.get("doc_id") or path.stem
    # 整篇文档的指纹：内容没变就不用重新入库（增量更新，和你 rag 项目里的 md5 去重一个思路，但粒度是文档）
    content_hash = hashlib.md5(text.encode("utf-8")).hexdigest()

    chunks: list[Document] = []
    for section in header_splitter.split_text(body):
        if not section.metadata.get("section"):
            continue  # 第一个 "##" 之前的内容（文档开头的说明、免责声明）不入库，它们会干扰检索
        for piece in size_splitter.split_text(section.page_content):
            metadata: dict[str, Any] = {
                "doc_id": doc_id,
                "title": meta.get("title", doc_id),
                "city": meta.get("city", ""),
                "type": meta.get("type", "tips"),
                "attraction": meta.get("attraction", ""),
                "closed_weekdays": meta.get("closed_weekdays", ""),
                "updated_at": meta.get("updated_at", ""),
                "section": section.metadata.get("section", ""),
                "content_hash": content_hash,
            }
            chunks.append(Document(id=f"{doc_id}#{len(chunks)}", page_content=piece, metadata=metadata))
    
    return chunks

def load_all(docs_dir: Path = DOCS_DIR) -> list[list[Document]]:
    """每篇文档一组块"""
    return [load_document(path) for path in sorted(docs_dir.glob("*.md"))]

if __name__ == "__main__":
    a = load_all()
    print(a)
