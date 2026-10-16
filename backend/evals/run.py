"""评测主程序：在评测集上跑一遍规划图，计算每个用例的分数，输出汇总报告

用法：
  uv run python -m evals.run --variant baseline                       # 用 .env 里的模型
  uv run python -m evals.run --variant deepseek --model deepseek-chat \\
      --base-url https://api.deepseek.com --api-key-env DEEPSEEK_API_KEY
  uv run python -m evals.run --variant quick --limit 5                # 只跑前 5 条，调试用
  uv run python -m evals.run --variant baseline --judge               # 加上 LLM 评委
  uv run python -m evals.run --variant ci --min-success 0.9           # 成功率低于 90% 时退出码为 1（给 CI 用）
"""

import argparse
import json
import os
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

from langchain_core.callbacks import get_usage_metadata_callback
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from app.config import get_settings
from app.graph.state import PlannerDeps
from app.graph.trip_graph import trip_graph
from app.observability.logs import setup_logging
from app.observability.tracing import langchain_callbacks, score_trace, trace_span
from app.services.llm_service import get_llm
from evals.dataset import RESULTS_DIR, load_cases, load_context
from evals.evaluators import CaseOutput, evaluate
from evals.judge import judge_plan


def make_llm(model: str | None, base_url: str | None, api_key_env: str | None) -> BaseChatModel:
    """不传参数时用 .env 里的模型；传了就按参数创建，用来和别的模型做对比"""
    if not model:
        return get_llm()
    settings = get_settings()
    return ChatOpenAI(
        model=model,
        base_url=base_url or settings.llm_base_url,
        # os.environ[...]：从环境变量读取另一家的 Key，不要把 Key 写在命令行里（会留在 shell 历史记录中）
        api_key=os.environ[api_key_env] if api_key_env else settings.llm_api_key,
        timeout=settings.llm_timeout,
        max_retries=1,
    )


def run_case(case: dict[str, Any], llm: BaseChatModel, judge_llm: BaseChatModel | None, variant: str) -> dict[str, Any]:
    """跑一个用例，返回一行结果"""
    request, context = case["request"], load_context(case["id"])
    if context is None:
        raise SystemExit(f"用例 {case['id']} 没有冻结的 context，先运行 python -m evals.record_contexts")

    started = time.perf_counter()  # 高精度计时器，相当于前端的 performance.now()
    with (
        trace_span(f"eval/{case['id']}", tags=["eval", variant], input=request.model_dump(mode="json")) as span,
        # 统计这个 with 块里所有模型调用的 token 用量（按模型名分别累计）
        get_usage_metadata_callback() as usage_cb,
    ):
        final = trip_graph.invoke(
            {"request": request, "context": context},  # 带上冻结的 context，图会跳过 collect（P3 加的 route_start）
            config={"callbacks": [usage_cb, *langchain_callbacks()], "recursion_limit": 20},
            context=PlannerDeps(amap=None, llm=llm),  # 不会调用高德，所以 amap 传 None
        )
    latency = time.perf_counter() - started

    out = CaseOutput(request, context, final["plan"], final["source"], final.get("attempts", 0))
    scores = evaluate(out)
    judge_reason = None
    if judge_llm is not None and out.source == "llm":
        try:
            verdict = judge_plan(judge_llm, request, out.plan)
            scores |= verdict.normalized()  # |= 把评委的分数合并进来
            judge_reason = verdict.reason
        except Exception as exc:  # 评委自己也可能失败，不能因此丢掉整条结果
            judge_reason = f"评委失败: {exc}"

    # 把分数写回 Langfuse 的 trace：在界面上能按分数筛选，比如"找出所有 grounding < 0.8 的用例"
    if span is not None:
        for name, value in scores.items():
            if value is not None:
                score_trace(span.trace_id, name, value)

    usage = usage_cb.usage_metadata  # {"模型名": {"input_tokens": ..., "output_tokens": ...}}
    row = {
        "id": case["id"],
        "tags": case["tags"],
        "variant": variant,
        "source": out.source,
        "attempts": out.attempts,
        "latency_s": round(latency, 2),
        "input_tokens": sum(u["input_tokens"] for u in usage.values()),
        "output_tokens": sum(u["output_tokens"] for u in usage.values()),
        "scores": scores,
        "errors": final.get("errors", []),
        "judge_reason": judge_reason,
    }
    mark = "✅" if out.source == "llm" else "❌"
    print(f"{mark} {case['id']:<20} {latency:6.1f}s  尝试 {out.attempts} 次", flush=True)
    return row


def percentile(values: list[float], p: float) -> float:
    """第 p 百分位数，例如 p=95：95% 的请求都比它快"""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))]


def summarize(rows: list[dict[str, Any]], price_in: float, price_out: float) -> dict[str, Any]:
    """汇总：每个指标取平均（跳过不适用的 None），再加上延迟和成本"""
    metric_names = rows[0]["scores"].keys()
    summary: dict[str, Any] = {"cases": len(rows)}
    for name in metric_names:
        values = [r["scores"].get(name) for r in rows if r["scores"].get(name) is not None]
        summary[name] = round(statistics.mean(values), 3) if values else None
    latencies = [r["latency_s"] for r in rows]
    summary["latency_p50_s"] = round(statistics.median(latencies), 1)
    summary["latency_p95_s"] = round(percentile(latencies, 95), 1)
    summary["avg_input_tokens"] = round(statistics.mean(r["input_tokens"] for r in rows))
    summary["avg_output_tokens"] = round(statistics.mean(r["output_tokens"] for r in rows))
    # 价格单位：元 / 百万 token
    summary["avg_cost_yuan"] = round(
        (summary["avg_input_tokens"] * price_in + summary["avg_output_tokens"] * price_out) / 1_000_000, 4
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--variant", required=True, help="这次评测的名字，例如 baseline、qwen-max、prompt-v2")
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-env", help="存放 API Key 的环境变量名")
    parser.add_argument("--judge", action="store_true", help="启用 LLM 评委")
    parser.add_argument("--judge-model", help="评委模型，默认和被评测的模型相同（不推荐）")
    parser.add_argument("--judge-base-url")
    parser.add_argument("--judge-api-key-env")
    parser.add_argument("--ids", help="只跑这些用例，逗号分隔")
    parser.add_argument("--tags", help="只跑带这些标签的用例，逗号分隔")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=2, help="并发数，太大容易被模型供应商限流")
    parser.add_argument("--price-in", type=float, default=0.0, help="输入价格：元 / 百万 token")
    parser.add_argument("--price-out", type=float, default=0.0, help="输出价格：元 / 百万 token")
    parser.add_argument("--min-success", type=float, help="成功率低于这个值时，退出码为 1")
    args = parser.parse_args()
    setup_logging("WARNING")  # 评测时只看警告和错误，重试原因会显示出来

    split = lambda s: set(s.split(",")) if s else None  # noqa: E731
    cases = load_cases(ids=split(args.ids), tags=split(args.tags))[: args.limit]
    llm = make_llm(args.model, args.base_url, args.api_key_env)
    judge_llm = None
    if args.judge:
        judge_llm = make_llm(args.judge_model, args.judge_base_url, args.judge_api_key_env) if args.judge_model else llm

    print(f"🧪 评测 {args.variant}：{len(cases)} 个用例，并发 {args.workers}")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        # pool.map 保持原来的顺序返回结果，相当于 Promise.all(cases.map(...))
        rows = list(pool.map(lambda c: run_case(c, llm, judge_llm, args.variant), cases))

    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / f"{args.variant}_{datetime.now():%Y%m%d-%H%M%S}.jsonl"
    out_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")

    summary = summarize(rows, args.price_in, args.price_out)
    print("\n| 指标 | 值 |\n|---|---|")
    for key, value in summary.items():
        print(f"| {key} | {value} |")
    print(f"\n结果已保存：{out_path}")

    if args.min_success is not None and summary["success"] < args.min_success:
        print(f"❌ 成功率 {summary['success']} 低于门槛 {args.min_success}")
        sys.exit(1)  # 非 0 退出码 = 失败，CI 会把这次构建标红


if __name__ == "__main__":
    main()
