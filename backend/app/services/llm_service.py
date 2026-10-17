"""大模型服务：创建并共享一个 ChatOpenAI 实例"""

import logging
from functools import lru_cache

from langchain_openai import ChatOpenAI

from app.config import get_settings

logger = logging.getLogger(__name__)

class LLMNotConfiguredError(Exception):
    """没有配置大模型 Key 时抛出"""
    
# 单例：整个进程共用一个 LLM 客户端
@lru_cache(maxsize=1)  # 缓存函数返回值，maxsize=1 表示只缓存一个实例
def get_llm() -> ChatOpenAI:
    settings = get_settings()
    if not (settings.llm_api_key and settings.llm_base_url and settings.llm_model_id):
        raise LLMNotConfiguredError("LLM_API_KEY / LLM_BASE_URL / LLM_MODEL_ID 未配置完整")
    
    # 显式把配置传进去，而不是让 ChatOpenAI 自己去读 OPENAI_API_KEY 等环境变量：
    # 配置只有 config.py 一个来源，排查问题时不用猜"它到底读的是哪个变量"
    llm = ChatOpenAI(
        model=settings.llm_model_id,
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        timeout=settings.llm_timeout,
        # SDK 自带的重试：遇到 429 限流、5xx、网络抖动时自动重发（默认 2 次）
        # 设成 1：第 7 课我们会自己写"格式不合格就重试"的循环，两层重试叠加，最坏情况会等很久
        max_retries=1,
        # P4：流式调用时也返回 token 用量（请求里带上 stream_options.include_usage）
        # 只有官方 OpenAI 地址才默认开启；百炼、DeepSeek 等自定义 base_url 默认是关的，
        # 不开的话，改成流式之后 Langfuse 的成本、评测的 token 统计都会变成 0
        stream_usage=True,
    )
    logger.info("LLM 初始化成功: model=%s, base_url=%s", llm.model_name, settings.llm_base_url)
    return llm