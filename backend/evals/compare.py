"""对比两次评测结果，输出 Markdown 报告

用法：uv run python -m evals.compare evals/results/baseline_xxx.jsonl evals/results/deepseek_xxx.jsonl
"""

import argparse
import json
from pathlib import Path

from evals.run import summarize


def load_rows(path: str) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline")
    parser.add_argument("candidate")
    parser.add_argument("--price-in-a", type=float, default=0.0)
    parser.add_argument("--price-out-a", type=float, default=0.0)
    parser.add_argument("--price-in-b", type=float, default=0.0)
    parser.add_argument("--price-out-b", type=float, default=0.0)
    args = parser.parse_args()

    rows_a, rows_b = load_rows(args.baseline), load_rows(args.candidate)
    a = summarize(rows_a, args.price_in_a, args.price_out_a)
    b = summarize(rows_b, args.price_in_b, args.price_out_b)
    name_a, name_b = rows_a[0]["variant"], rows_b[0]["variant"]

    print(f"## {name_a} vs {name_b}\n")
    print(f"| 指标 | {name_a} | {name_b} | 变化 |\n|---|---|---|---|")
    for key in a:
        va, vb = a[key], b.get(key)
        delta = f"{vb - va:+.3f}" if isinstance(va, (int, float)) and isinstance(vb, (int, float)) else ""
        print(f"| {key} | {va} | {vb} | {delta} |")

    # 逐个用例找"退步"：平均分可能掩盖个别用例的严重问题
    by_id = {r["id"]: r for r in rows_a}
    print("\n### 退步的用例\n")
    found = False
    for rb in rows_b:
        ra = by_id.get(rb["id"])
        if ra is None:
            continue
        for metric, vb in rb["scores"].items():
            va = ra["scores"].get(metric)
            if va is not None and vb is not None and va - vb >= 0.2:
                print(f"- `{rb['id']}` {metric}: {va:.2f} → {vb:.2f}")
                found = True
    if not found:
        print("（无）")


if __name__ == "__main__":
    main()
