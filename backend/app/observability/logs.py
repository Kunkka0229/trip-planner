"""日志：每一行都带上 request_id，一次请求的所有日志可以用它串起来

为什么不用 print？
  - print 没有级别：线上想只看 WARNING 以上的，做不到
  - print 没有上下文：10 个用户同时请求，日志交错在一起，分不清哪行是谁的
  - print 不能统一格式：日志平台（ELK、Loki、阿里云 SLS）需要固定格式才能检索
"""

import logging
from contextvars import ContextVar

# ContextVar：每个请求（准确说是每个"执行上下文"）各自独立的变量
# 两个请求同时在跑，各自 set 自己的值，互不覆盖。前端没有完全对应的东西，
# 最接近的是 Node 的 AsyncLocalStorage：在异步调用链里隐式传递的"请求级全局变量"
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

class RequestIdFilter(logging.Filter):
    """日志过滤器：每条日志输出前，把当前的 request_id 塞进 record，格式字符串里就能用 %(request_id)s"""
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True  # 返回 True 表示这条日志照常输出
    
def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()  # 输出到终端（stderr）
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    root = logging.getLogger()  # 根 logger：所有 logging.getLogger(__name__) 创建的 logger 最终都会交给它输出
    root.handlers = [handler]  # 覆盖掉默认的 handler，避免重复输出
    root.setLevel(level.upper())
    # 第三方库的日志太吵，只看警告以上
    for noisy in ("httpx", "httpcore", "openai", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)