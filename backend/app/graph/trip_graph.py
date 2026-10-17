"""规划主图：把第 7、9 课的"收集 → 生成 → 校验 → 重试 / 兜底"画成一张状态图

START → collect ──有景点──→ generate ──有输出──→ validate ──通过──→ END
           │                  ↑   │                │
           │                  │   └──调用失败──┐   │ 不通过
           │                  └────还能重试────┴───┘
           │                                   │ 次数用完
           └──没有景点──────→ fallback ←────────┘ → END
"""

import json
import logging
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.utils.json import parse_partial_json
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from app.agents.generation import BASE_TEMPERATURE, MAX_ATTEMPTS, build_fallback
from app.agents.output import parse_and_check, recompute_budget
from app.agents.prompts import PLANNER_SYSTEM_PROMPT, build_context_prompt
from app.graph.collect import build_collect_graph
from app.graph.state import PlannerDeps, TripState
from app.models.schemas import WeatherInfo
from app.planner.context import compact_for_prompt
from app.planner.grounding import ground_plan

logger = logging.getLogger(__name__)  # __name__ 是模块名，例如 app.graph.trip_graph，日志里会显示出来

def error_reason(exc: Exception) -> str:
    """异常 → 一行简短的原因（和第 7 课 generate_plan_with_retries 里的写法一样）"""
    return f"{type(exc).__name__}: {str(exc).splitlines()[0]}"

# ---------- 节点：每个节点接收 state，返回"要更新的字段" ----------
def finished_days(text: str) -> list[dict[str, Any]]:
    """P4：从还没生成完的 JSON 里，解析出已经"写完"的那几天

    parse_partial_json 能解析不完整的 JSON（自动补齐缺失的括号和引号），例如
        '{"days": [{"date": "a"}, {"date": "b", "mea'  →  {"days": [{"date": "a"}, {"date": "b"}]}
    最后一天可能还没写完，所以只返回"后面已经开始写下一天"的那些天
    """
    start = text.find("{")
    if start == -1:
        return []
    try:
        data = parse_partial_json(text[start:])
    except Exception:  # 前面有不规范的内容，暂时解析不了，等更多内容到达再试
        return []
    days = data.get("days") if isinstance(data, dict) else None
    return days[:-1] if isinstance(days, list) else []

async def generate(state: TripState, runtime: Runtime[PlannerDeps]) -> dict[str, Any]:
    """调用一次大模型，只负责拿到原始输出，不做校验

    P4：改成流式调用。模型每吐出一段文字就检查一次，有一天写完了，就立刻把这一天推给前端
    用户在 30 秒的等待里能看到行程一天一天地"长出来"，而不是盯着进度条
    """
    
    writer = get_stream_writer()
    attempt = state.get("attempts", 0) + 1
    context_json = json.dumps(compact_for_prompt(state["context"]), ensure_ascii=False, separators=(",", ":"))
    prompt = build_context_prompt(context_json, state["request"].travel_days)
    if errors := state.get("errors"):
        prompt += f"\n\n注意：上一次的输出不合格，原因是：{errors[-1]}。请修正后重新输出完整 JSON。"
        writer({"type": "reset"})  # 重试：让前端清掉上一次的草稿
    
    writer({"type": "progress", "stage": "正在生成行程" if attempt == 1 else f"正在重新生成（第 {attempt} 次）",
            "percent": 40 + 10 * (attempt - 1)})
    temperature = BASE_TEMPERATURE + 0.1 * (attempt - 1)
    messages = [SystemMessage(PLANNER_SYSTEM_PROMPT), HumanMessage(prompt)]
    parts: list[str] = []
    sent = 0  # 已经推送了几天
    
    try:
        # astream：异步流式调用，每次产出一小段（chunk），相当于前端用 fetch 读 ReadableStream
        async for chunk in runtime.context.llm.astream(messages, temperature=temperature):
            parts.append(chunk.text)
            if "}" not in chunk.text:  # 只有出现右括号时，才可能有一天刚写完，省掉大部分解析
                continue
            
            days = finished_days("".join(parts))
            for index in range(sent, len(days)):
                writer({"type": "day", "index": index, "day": days[index]})
            sent = max(sent, len(days))
    except Exception as exc:  # 超时、网络错误：记一次失败，draft 置空
        logger.warning("第 %d/%d 次调用失败: %s", attempt, MAX_ATTEMPTS, error_reason(exc))
        return {"attempts": attempt, "draft": "", "errors": [error_reason(exc)]}
    
    return {"attempts": attempt, "draft": "".join(parts)}

def validate(state: TripState) -> dict[str, Any]:
    """校验 + grounding + 天气覆盖 + 预算重算（原来 context_planner 里 postprocess 闭包做的事）"""
    request, context = state["request"], state["context"]
    try:
        plan = parse_and_check(state["draft"], request)
        stats = ground_plan(plan, context)
        plan.weather_info = [WeatherInfo(**w) for w in context["tool_snapshot"]["trip_weather"]]
        plan.budget = recompute_budget(plan, request)
    except Exception as exc:
        logger.warning("第 %d/%d 次校验失败: %s", state["attempts"], MAX_ATTEMPTS, error_reason(exc))
        return {"errors": [error_reason(exc)]}
    
    logger.info("Grounding: 景点命中率 %.0f%%，未匹配 %s", stats["attraction_rate"] * 100, stats["ungrounded"])
    status = context["tool_status"]
    warnings = [f"{name} 数据获取失败" for name, s in status.items() if not s["ok"]]
    warnings += [f"{name} 不在高德候选中" for name in dict.fromkeys(stats["ungrounded"])]
    return {"plan": plan, "source": "llm", "warnings": warnings}

def fallback(state: TripState) -> dict[str, Any]:
    request = state["request"]
    if state.get("attempts", 0) == 0:  # 一次都没调用大模型，说明是景点数据缺失
        error = state["context"]["tool_status"].get("attractions", {}).get("error", "没有搜到景点")
        reason = f"景点数据获取失败: {error}"
        return {"plan": build_fallback(request, reason), "source": "fallback", "errors": [reason]}
    logger.error("大模型多次生成失败，返回兜底方案")
    return {"plan": build_fallback(request, "大模型多次生成失败"), "source": "fallback"}

# ---------- 路由函数：读 state，返回下一个节点的名字 ----------
# 返回值类型写成 Literal：LangGraph 会据此知道"可能去哪些节点"，画图时能画出所有分支
def route_start(state: TripState) -> Literal["collect", "generate", "fallback"]:
    """P3：调用方已经带了 context（评测时用冻结的 context）就跳过 collect，直接进入后面的流程"""
    return route_after_collect(state) if state.get("context") else "collect"

def route_after_collect(state: TripState) -> Literal["generate", "fallback"]:
    # 景点是行程的核心：没有景点候选就不调用大模型（第 9 课的规则）
    return "generate" if state["context"]["tool_snapshot"]["attraction_pois"] else "fallback"

def retry_or_give_up(state: TripState) -> Literal["generate", "fallback"]:
    return "generate" if state["attempts"] < MAX_ATTEMPTS else "fallback"

def route_after_generate(state: TripState) -> Literal["validate", "generate", "fallback"]:
    return "validate" if state["draft"] else retry_or_give_up(state)

def route_after_validate(state: TripState) -> Literal["generate", "fallback", "__end__"]:
    # "__end__" 就是 END 常量的值；plan 存在说明校验通过
    return END if state.get("plan") else retry_or_give_up(state)

def build_trip_graph():
    builder = StateGraph(TripState, context_schema=PlannerDeps)
    # 编译好的子图可以直接当成一个节点：主图只看到它的输入（request）和输出（context）
    builder.add_node("collect", build_collect_graph())
    builder.add_node("generate", generate)
    builder.add_node("validate", validate)
    builder.add_node("fallback", fallback)

    # P3：入口也可以是条件边。正常请求走 collect；评测时传入冻结的 context，直接去 generate
    builder.add_conditional_edges(START, route_start)
    # 条件边：执行完 collect 后，调用 route_after_collect，按返回值决定去哪
    # 相当于前端路由守卫：beforeEach((to) => hasData ? next() : next('/fallback'))
    builder.add_conditional_edges("collect", route_after_collect)
    builder.add_conditional_edges("generate", route_after_generate)
    builder.add_conditional_edges("validate", route_after_validate)
    builder.add_edge("fallback", END)
    return builder.compile()

# 图的结构是固定的，模块加载时编译一次，所有请求共用（依赖通过 context 每次传入）
trip_graph = build_trip_graph()