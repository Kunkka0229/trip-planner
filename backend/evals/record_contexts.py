"""冻结 PlannerContext：用真实高德把每个用例的 context 收集一次，存成文件

为什么要冻结？
  评测要回答的是"换了模型 / 改了 prompt，效果是变好还是变坏"。如果每次评测都重新调高德，
  搜索结果、天气每天都在变，分数的变化就分不清是"模型的原因"还是"数据的原因"
  冻结之后，输入完全固定，唯一的变量就是你要比较的那个东西（控制变量法）
  这也是第 9 课 PlannerContext 设计的红利："格式固定的完整输入快照"

用法：
  uv run python -m evals.record_contexts            # 只收集还没有冻结的用例
  uv run python -m evals.record_contexts --force    # 全部重新收集
"""

import argparse
import json

from app.planner.context import PlannerContextBuilder
from app.services.amap_client import get_amap_client
from evals.dataset import CONTEXTS_DIR, context_path, load_cases


def main() -> None:
    parser = argparse.ArgumentParser()  # 命令行参数解析，相当于 Node 的 commander / yargs
    parser.add_argument("--force", action="store_true", help="覆盖已有的 context")
    args = parser.parse_args()

    CONTEXTS_DIR.mkdir(exist_ok=True)
    builder = PlannerContextBuilder(get_amap_client())
    for case in load_cases():
        path = context_path(case["id"])
        if path.exists() and not args.force:
            continue
        context = builder.collect(case["request"])
        path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
        status = {name: s["ok"] for name, s in context["tool_status"].items()}
        print(f"✅ {case['id']}: 景点 {len(context['tool_snapshot']['attraction_pois'])} 个，工具状态 {status}")


if __name__ == "__main__":
    main()
