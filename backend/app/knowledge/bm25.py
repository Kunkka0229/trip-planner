"""BM25 关键词检索（不依赖第三方库，40 行）

为什么向量检索之外还要关键词检索？
  向量检索擅长"意思相近"：问"宝宝能去哪"能找到"适合婴儿车的公园"
  但不擅长"精确的词"：问"珍宝馆"，向量可能更偏向"博物馆、展览"这类泛泛的内容
  关键词检索正好相反。两者结合（混合检索）是 RAG 的标准做法

中文没有空格分词，这里用"相邻两个字"（bigram）当作词："珍宝馆" → ["珍宝", "宝馆"]
生产环境一般用分词器（jieba）或者直接用 Elasticsearch / OpenSearch 自带的 BM25
"""

import math
from collections import Counter

def tokenize(text: str) -> list[str]:
    """把文本切成 bigram（相邻两个字一组）："故宫 周一闭馆" → ["故宫", "宫周", "周一", "一闭", "闭馆"]

    文档入库和用户提问都用它切词，两边切法一致，才能按"共同的词"算相关度
    """
    # 先统一小写（"MCP" 和 "mcp" 算同一个词），再去掉空白和中英文标点：标点不带信息，留着只会产生"馆。"这种没用的词
    chars = [c for c in text.lower() if not c.isspace() and c not in "，。、！？；：“”‘’（）()《》,.!?;:\"'"]
    # 相邻两个字拼成一个词（滑动窗口，每次右移一个字）
    # 只有 1 个字时 bigram 是空列表，用 or 退回成单字，至少不是空的
    # 但文档是按 bigram 入库的，单字提问（比如"鸭"）照样匹配不到"烤鸭"，这是 bigram 的局限，这种情况靠向量检索补上
    # 去掉标点后再配对，所以标点两侧的字也会连成一个词（"闭馆。周二" → "馆周"），多出几个无用的词，对打分影响不大
    return [chars[i] + chars[i + 1] for i in range(len(chars) - 1)] or chars

class BM25:
    """给一组文档建关键词索引，按查询给每篇文档打相关度分

    一篇文档的得分 = 查询里每个词的得分之和，每个词的得分取决于三件事：
      - 这个词有多少见（IDF）：每篇都有的词（"北京"）几乎不加分，少见的词（"珍宝馆"）加分多
      - 这篇文档里出现了几次（TF）：越多越相关，但有上限，出现 10 次不等于比 1 次相关 10 倍
      - 这篇文档有多长：长文档自然容易包含更多词，要打个折扣，避免长文档总是排在前面

    索引在创建时一次算好，之后每次查询只做查表和加法；文档变了要重新创建
    （前端类比：像 Fuse.js 这类搜索库，先 new 出索引再 search）
    """

    def __init__(self, texts: list[str], k1: float = 1.5, b: float = 0.75):
        # k1：词频的饱和速度。越小，词频多出现几次带来的加分越快到顶；常用 1.2 ~ 2.0
        # b：文档长度的影响程度。0 = 不管长短，1 = 完全按长度折算；常用 0.75
        # 每篇文档 → {词: 出现次数}，例如 {"故宫": 3, "闭馆": 1, ...}（Counter 是计数用的 dict）
        self.docs = [Counter(tokenize(t)) for t in texts]
        self.lengths = [sum(d.values()) for d in self.docs]  # 每篇文档的总词数
        # 平均长度：判断一篇文档"算长还是算短"的基准。没有文档时是 0（这时 scores 里不会进入除法）
        self.avg_len = sum(self.lengths) / len(self.lengths) if self.lengths else 0
        self.k1, self.b = k1, b
        # 每个词出现在多少篇文档里：越少见的词，区分度越高（IDF）
        # 注意 for term in d 遍历的是 key，所以一篇文档里出现多次只算 1 篇
        df = Counter(term for d in self.docs for term in d)
        n = len(self.docs)
        # IDF 公式（Lucene / Elasticsearch 用的版本）：f 越小值越大
        # 外面的 1 + 保证结果总是正数：原始 BM25 里出现在一半以上文档中的词 IDF 是负数，会倒扣分
        self.idf = {term: math.log(1 + (n - f + 0.5) / (f + 0.5)) for term, f in df.items()}

    def scores(self, query: str) -> list[float]:
        """返回每篇文档的得分，顺序和创建时传入的 texts 一致（0 分 = 没有任何共同的词）

        只打分不排序：混合检索时要和向量检索的结果合并，由调用方决定怎么排
        """
        terms = tokenize(query)
        result = []
        for doc, length in zip(self.docs, self.lengths):
            score = 0.0
            for term in terms:
                tf = doc.get(term, 0)  # 这个词在这篇文档里出现的次数
                if tf:  # 没出现的词贡献 0 分；也顺带避开了查询里有、索引里没有的词（self.idf 里查不到）
                    # BM25 公式：词频越高分越高，但有上限（k1 控制）；文档越长，词频的价值越低（b 控制）
                    score += self.idf[term] * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / self.avg_len))
            result.append(score)
        return result
    
if __name__ == "__main__":
    # 手动试一下：uv run python app/knowledge/bm25.py（或者在编辑器里直接运行这个文件）
    print(tokenize("故宫 周一闭馆。"))

    texts = [
        "故宫博物院每周一闭馆（法定节假日除外），珍宝馆需要另外购票",
        "国家博物馆周一闭馆，参观需要提前预约",
        "西湖免费开放，断桥、苏堤适合清晨散步",
        "北京烤鸭推荐四季民福、大鸭梨，人均 100 元左右",
    ]
    bm25 = BM25(texts)
    for query in ["珍宝馆要买票吗", "周一闭馆", "烤鸭", "鸭", "上海迪士尼"]:
        scores = bm25.scores(query)
        # 按分数从高到低取前 2 篇，0 分的不要（没有共同的词）
        top = sorted(((s, t) for s, t in zip(scores, texts) if s > 0), reverse=True)[:2]
        print(f"\n{query} → {tokenize(query)}")
        for score, text in top:
            print(f"  {score:.3f}  {text[:20]}")
        if not top:
            print("  （没有匹配）")
    