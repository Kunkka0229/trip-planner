"""把 knowledge/docs/ 下的文档写入知识库

用法：uv run python -m app.knowledge.ingest
文档改了、新增了、删除了，都重新跑一次，只会处理有变化的部分
"""

from app.knowledge.loader import load_all
from app.knowledge.service import get_knowledge_base

def main() -> None:
    kb = get_knowledge_base()
    if kb is None:
        raise SystemExit("没有配置 EMBEDDING_MODEL，无法建立知识库")
    documents = load_all()
    stats = kb.ingest(documents)
    removed = kb.remove_missing({chunks[0].metadata["doc_id"] for chunks in documents if chunks})
    print(f"✅ 新增 {stats['added']} 块，跳过未变化的文档 {stats['skipped']} 篇，删除旧块 {stats['removed'] + removed} 个")
    
if __name__ == "__main__":
    main()