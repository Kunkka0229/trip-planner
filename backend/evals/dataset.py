"""评测集的读取，以及冻结 context 的存取"""

import json
from pathlib import Path
from typing import Any

from app.models.schemas import TripRequest

EVALS_DIR = Path(__file__).resolve().parent
DATASET_PATH = EVALS_DIR / "dataset.jsonl"
CONTEXTS_DIR = EVALS_DIR / "contexts"  # 冻结的 PlannerContext，每个用例一个 JSON 文件
RESULTS_DIR = EVALS_DIR / "results"  # 每次评测的结果


def load_cases(ids: set[str] | None = None, tags: set[str] | None = None) -> list[dict[str, Any]]:
    """读取评测集。jsonl = 每行一个 JSON 对象，方便追加、方便 git diff 看出改了哪一条"""
    cases = []
    for line in DATASET_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        case = json.loads(line)
        case["request"] = TripRequest.model_validate(case["request"])  # 评测集本身也要过校验，写错了立刻发现
        if ids and case["id"] not in ids:
            continue
        if tags and not tags & set(case["tags"]):  # & 是集合交集：有任意一个标签匹配就保留
            continue
        cases.append(case)
    return cases


def context_path(case_id: str) -> Path:
    return CONTEXTS_DIR / f"{case_id}.json"


def load_context(case_id: str) -> dict[str, Any] | None:
    path = context_path(case_id)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
