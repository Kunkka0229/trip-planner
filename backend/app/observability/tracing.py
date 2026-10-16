"""Langfuse 链路追踪：一次规划请求 = 一条 trace，里面每个图节点、每次大模型调用都是一个 span

trace（链路）
└── trip_plan                     ← 我们手动创建的根 span，带 user_id、tags、输入输出
    └── trip_graph                ← LangGraph 整张图（CallbackHandler 自动创建）
        ├── collect
        │   ├── attractions / hotels / foods / weather
        │   └── assemble
        ├── generate
        │   └── ChatOpenAI        ← generation：prompt、回复、token 数、耗时、成本
        └── validate

没配置 Langfuse 的 Key 时，所有函数都退化成"什么都不做"，不影响业务（和第 6 课没配 LLM 时退回 mock 是一个思路）
"""

import logging
from collections.abc import Generator  # 生成器类型，给 @contextmanager 函数标注返回类型用
from contextlib import contextmanager  # 把"带一个 yield 的函数"变成可以写在 with 后面的上下文管理器
from functools import lru_cache  # 缓存函数返回值：第一次调用后结果被记住，之后直接返回（单例）
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler  # LangChain 回调的基类，只用来标注返回类型
from langfuse import Langfuse, propagate_attributes
from langfuse.langchain import CallbackHandler  # Langfuse 提供的 LangChain 回调：把图和模型的每一步上报成 span

from app.config import get_settings
from app.observability.logs import request_id_var  # 当前请求的 request_id（ContextVar），用来生成 trace_id

# __name__ = "app.observability.tracing"；日志会交给 logs.py 配置好的根 logger 统一输出
logger = logging.getLogger(__name__)

# @lru_cache：整个进程只创建一个 Langfuse 客户端，所有请求共用（和 get_settings、get_llm 一样的单例写法）
# 返回 None 表示没配置 Langfuse，下面的每个函数都先检查它，None 时就什么都不做
@lru_cache
def get_langfuse() -> Langfuse | None:
    settings = get_settings()
    # 两个 Key 缺一个都不启用：本地开发不配 Langfuse 也能正常运行
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    # 因为有 lru_cache，这行日志只会在第一次调用时打印一次，看到它就说明 Langfuse 启用成功了
    logger.info("Langfuse 已启用: %s", settings.langfuse_base_url)
    # Langfuse 客户端在后台线程里攒一批数据再上报（批量 + 异步），不会拖慢请求
    return Langfuse(
        public_key=settings.langfuse_public_key,  # pk-lf-...，标识是哪个项目
        secret_key=settings.langfuse_secret_key,  # sk-lf-...，相当于密码，不能泄露
        base_url=settings.langfuse_base_url,  # 自部署填 http://localhost:3000，官方云服务填 https://cloud.langfuse.com
        environment=settings.app_env,  # dev / prod 的数据分开看
        release=settings.app_version,  # 按版本对比效果：新版本上线后成功率是升了还是降了
    )
    
def langchain_callbacks() -> list[BaseCallbackHandler]:
    """传给 LangChain / LangGraph 的回调。CallbackHandler 会把图的每一步、每次模型调用都上报

    用法：trip_graph.stream(..., config={"callbacks": langchain_callbacks()})
    只需要在最外层传一次：LangChain 会把 config 沿着调用链往下传，图里的每个节点、
    节点里的 llm.invoke(...) 都会被记录，节点代码一行都不用改（"零侵入"）
    """
    langfuse = get_langfuse()
    if langfuse is None:
        return []  # 返回空列表而不是 None：调用方直接放进 config 就行，不用再判断
    # public_key 用来找到上面创建的那个客户端（同一个进程里可以有多个 Langfuse 项目）
    return [CallbackHandler(public_key=get_settings().langfuse_public_key)]

@contextmanager
def trace_span(
    name: str,                         # 根 span 的名字，必填，例如 "trip_plan"、"trip_chat"
    *,                                 # 分隔符：它后面的参数只能用"名字=值"的方式传
    user_id: str | None = None,        # 哪个用户的请求（来自请求头 X-User-Id），Langfuse 的 Users 页面按它统计
    session_id: str | None = None,     # 属于哪个会话（P5 开始传 thread_id），同一会话的多条 trace 归到一个 Session
    tags: list[str] | None = None,     # 标签，用来筛选、对比，例如 ["graph"]、["eval", "baseline"]
    input: Any = None                  # 这次请求的输入，点开 trace 时最上面显示
) -> Generator[Any, None, None]:       # Generator[yield 出去的类型, send 的类型, return 的类型]，后两个用不到写 None
    """创建根 span，并把 user_id / session_id / tags 传播给它下面的所有 span

    用法：
        with trace_span("trip_plan", user_id="u1", input=...) as span:
            ...                          # 这里面的大模型调用都会挂到这条 trace 下
            if span: span.update(output=...)

    执行顺序（yield 是分界线）：
        yield 之前：进入调用方的 with 时执行，创建根 span、开始传播属性
        yield span：把 span 交给调用方（就是 as 后面的变量），暂停在这里，执行调用方 with 块里的代码
        yield 之后：调用方的 with 块结束后回到这里，内层 with 自动退出：span 结束、记录耗时、交给后台上报
    """
    langfuse = get_langfuse()
    if langfuse is None:
        # 没配置 Langfuse：交出 None，调用方用 if span: 判断后跳过，业务照常执行
        yield None
        return  # 生成器里的 return：到此结束，不会再执行下面的代码
    
    # 用 request_id 当种子生成 trace_id：同一个 request_id 永远得到同一个 trace_id
    # 好处：日志里看到 request_id，就能直接算出 trace 的地址，反过来也一样
    # request_id 是 "-"（默认值）说明不在请求里（比如评测脚本），这时随机生成一个
    request_id = request_id_var.get()
    trace_id = Langfuse.create_trace_id(seed=request_id) if request_id != "-" else Langfuse.create_trace_id()

    # with (A as a, B)：同时进入两个上下文管理器，相当于嵌套两层 with
    #   进入顺序 A → B，退出顺序 B → A（像括号一样成对关闭）
    with (
        # A：创建根 span，并把它设成"当前 span"（current，内部用 ContextVar 记录）
        #   这个 with 块里 CallbackHandler 创建 trip_graph、generate 等 span 时，
        #   会先查"当前 span 是谁"，然后自动挂到它下面，这样才形成一棵完整的树
        #   as_type="agent"：span 的类型，Langfuse 界面上按类型显示不同的图标
        #   trace_context：指定 trace_id（上面由 request_id 算出来的），不让 Langfuse 随机生成
        langfuse.start_as_current_observation(
            name=name, as_type="agent", input=input, trace_context={"trace_id": trace_id}
        ) as span,
        # B：propagate_attributes：在这个 with 块里创建的所有 span 都自动带上这些属性
        #   这样在 Langfuse 里按用户筛选时，能筛出完整的链路，而不只是根 span
        #   trace_name：整条 trace 在列表里显示的名字，和根 span 同名
        propagate_attributes(user_id=user_id, session_id=session_id, tags=tags, trace_name=name),
    ):
        # 在 A、B 都生效的状态下，把 span 交给调用方执行它的代码
        # 调用方的 with 块结束后回到这里 → B 退出（停止传播）→ A 退出（span 结束，恢复之前的当前 span）
        yield span
        
def score_trace(trace_id: str, name: str, value: float, comment: str = "", data_type: str = "NUMERIC") -> bool:
    """给一条 trace 打分（用户反馈、自动评测结果都用它），返回是否成功提交"""
    langfuse = get_langfuse()
    if langfuse is None:
        return False  # 没配置 Langfuse：分数没地方存，告诉调用方"没记录上"
    
    # trace_id：给哪条 trace 打分（前端提交反馈时带回来的，或评测脚本里 span.trace_id）
    # name：分数的名字，例如 "user_feedback"、"grounding"，Langfuse 里按名字分别统计
    # comment or None：空字符串换成 None，Langfuse 里就不会显示一条空评论
    # data_type：NUMERIC 是任意数值（评测分数）；BOOLEAN 只能是 0 / 1（👍 / 👎），界面上会显示成"好评率"
    # create_score 也是先放进后台队列再批量上报，不会阻塞请求
    langfuse.create_score(trace_id=trace_id, name=name, value=value, comment=comment or None, data_type=data_type)
    return True

def shutdown_tracing() -> None:
    """服务关闭前调用：把还没上报的数据全部发出去，否则最后几条 trace 会丢"""
    langfuse = get_langfuse()
    if langfuse is not None:
        langfuse.shutdown()

