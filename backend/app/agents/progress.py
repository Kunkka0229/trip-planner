"""进度回调的类型定义（第 11 课）"""

from typing import Callable

# 进度回调：on_progress("正在搜索景点", 20)
# 规划器在关键步骤调用它，至于进度怎么展示（打印日志、推给前端……）由调用方决定
ProgressFn = Callable[[str, int], None]

def no_progress(stage: str, percent: int) -> None:
    """默认的回调：什么都不做。这样规划器内部不用到处判断 if on_progress is not None"""
