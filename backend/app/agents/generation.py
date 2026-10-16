"""从第 7 课的 TripPlanner.plan 里抽出来的"生成 + 校验 + 重试 + 兜底"循环

为什么要抽出来？第 8 课的多 Agent 版本和第 7 课的直连版本，最后一步都是
"把一个 prompt 交给规划 Agent，拿到合格的 TripPlan"。同样的逻辑不应该写两遍
"""
import logging
from dataclasses import dataclass, field
from typing import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.output import parse_and_check, recompute_budget
from app.agents.prompts import PLANNER_SYSTEM_PROMPT
from app.agents.progress import ProgressFn, no_progress
from app.models.schemas import TripPlan, TripRequest
from app.services.mock_planner import build_mock_plan

logger = logging.getLogger(__name__)

# 最多调用大模型几次
MAX_ATTEMPTS = 3
# 大模型输出的随机性：temperature 越低输出越稳定；结构化输出一般用 0.2~0.5
BASE_TEMPERATURE = 0.3

@dataclass
class PlanResult:
    plan: TripPlan
    source: str  # "llm" | "fallback"
    attempts: int
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)  # 不影响使用、但值得告诉用户的问题

def build_fallback(request: TripRequest, reason: str) -> TripPlan:
    fallback = build_mock_plan(request)
    fallback.overall_suggestions = f"智能规划暂时不可用（{reason}），以下是基础行程模板，请稍后重试。"
    fallback.budget = recompute_budget(fallback, request)
    return fallback
    
def generate_plan_with_retries(
    llm: BaseChatModel, 
    base_prompt: str, 
    request: TripRequest,
    # 可选的后处理钩子：解析和校验通过后调用，可以修改 plan，返回值是警告列表
    # Callable[[参数类型], 返回类型]，相当于 TS 的 (plan: TripPlan) => string[]
    postprocess: Callable[[TripPlan], list[str]] | None = None,
    on_progress: ProgressFn = no_progress,  # 第 11 课：进度回调
) -> PlanResult:
    errors: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):  # 1, 2, 3
        prompt = base_prompt
        if errors:
            # 带错误提示重试：只把"上次错在哪"告诉模型，不把上次的整段输出塞回去
            # （整段输出很长，会让 prompt 越来越大，还可能让模型照着错误的格式继续写）
            prompt += f"\n\n注意：上一次的输出不合格，原因是：{errors[-1]}。请修正后重新输出完整 JSON。"
            
        # 每次重试稍微提高 temperature，让模型"换个思路"，避免反复犯同一个错误
        temperature = BASE_TEMPERATURE + 0.1 * (attempt - 1)
        # 大模型生成是最慢的一步，进度从 50% 开始，每次重试往后推一点
        on_progress("正在生成行程" if attempt == 1 else f"正在重新生成（第 {attempt} 次）", 50 + 10 * (attempt - 1))
        messages = [SystemMessage(PLANNER_SYSTEM_PROMPT), HumanMessage(prompt)]

        try:
            response = llm.invoke(messages, temperature=temperature).text
            # 解析和校验大模型输出
            plan = parse_and_check(response, request)
            warnings = postprocess(plan) if postprocess else []
            # 后处理可能修改了价格（grounding 用 hint 覆盖），所以预算要重新算一次
            plan.budget = recompute_budget(plan, request)
        except Exception as exc:  # 网络错误、超时、解析失败、校验失败……都算这一次失败
            # 只保留第一行，Pydantic 的错误信息可能很长
            reason = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            errors.append(reason)
            logger.warning("第 %d/%d 次失败: %s", attempt, MAX_ATTEMPTS, reason)
            continue
            
        logger.info("第 %d 次生成成功", attempt)
        return PlanResult(plan=plan, source="llm", attempts=attempt, errors=errors, warnings=warnings)
        
    # 所有重试都失败了：返回兜底方案，而不是直接报错。用户至少能看到一个能用的结果
    logger.error("大模型多次生成失败，返回兜底方案")
    return PlanResult(
        plan=build_fallback(request, "大模型多次生成失败"),
        source="fallback",
        attempts=MAX_ATTEMPTS,
        errors=errors,
    )

