# 第 9 课：重构为 PlannerContext 架构

**目标**：吸取第 8 课的教训，把规划流程重构成参考仓库现在的架构。这是整门课最重要的一课，学完你就能回答学习计划里的核心问题：**为什么参考仓库要从"多 Agent 各自调工具"改成"代码收集上下文 + 单 Agent 规划"？**

```
TripRequest
  │
  ├─① PlannerContextBuilder.collect      并行调用高德（景点 / 酒店 / 餐厅 / 天气）
  │      ├─ 每个候选附上价格提示（ticket_price_hint / meal_cost_hint / estimated_cost_hint）
  │      ├─ 天气按行程日期对齐，没有预报的日期明确标注"暂无预报"
  │      └─ tool_status 记录每个工具的成败       ← 错误不会再被吞掉
  │
  ├─② 景点数据都没拿到？→ 直接返回兜底方案，不让大模型凭空编
  │
  ├─③ compact_for_prompt → 规划 Agent（只调用 1 次大模型，失败才重试）
  │
  ├─④ parse_and_check（第 7 课）
  ├─⑤ ground_plan：匹配回真实候选，用真实数据覆盖坐标和价格；天气直接用代码对齐的结果
  └─⑥ recompute_budget
```

对应：参考仓库 `backend/app/planner/` 整个目录（`context.py`、`pricing.py`、`compact.py`、`output.py`），以及 `agents/trip_planner_agent.py`。

> 本课代码用假高德数据和假 LLM 测试了 3 种场景，门票价格表的查表逻辑另外测了 5 种情况（见 Step 1），高德限速用模拟 QPS 限制的假高德验证过（见 Step 2 的踩坑小节）：正常情况（模型改的价格被恢复、抄错的坐标被纠正、带分店后缀的名字能匹配、缺失的天气被补上，预算手工核对正确）；天气工具失败（生成照常进行，但提示里排在第一位）；景点工具失败（完全不调用大模型，直接返回兜底方案并说明原因）。**测试中还发现第 5 课的代码会把 API Key 泄露到错误信息里**，已经修复，见第 5 课讲义的"第四个坑"。

## 三种架构的对比（先理解"为什么"，再写代码）

| | 多 Agent + MCP（第 8 课） | 直连（第 6、7 课） | **PlannerContext（本课）** |
|---|---|---|---|
| 谁决定调用哪些工具 | 大模型 | 代码 | 代码 |
| 工具调用 | 并行（LangGraph 分支） | 依次执行 | **并行执行** |
| 数据形态 | 被大模型总结成自然语言 | 结构化 JSON | 结构化 JSON + **价格 hint** |
| 工具失败 | **被吞掉**，大模型继续编 | 整个请求报错 502 | **记录在 tool_status**，按严重程度分别处理 |
| 价格 | 大模型估 | 大模型估 | 代码给出 hint，**生成后再用 hint 覆盖一遍** |
| 输出可信度 | 无法验证 | 无法验证 | **grounding**：知道哪些内容来自真实数据 |
| 大模型调用次数 | 7 次以上 | 1 次 | 1 次 |
| 可审计 / 可训练 | 难 | 一般 | **context 是完整的输入快照**，可以存下来做评测、训练 |

最后一行是参考仓库的另一个重要动机：`PlannerContext` 是一个**格式固定的输入**，所以可以收集"context → 合格 TripPlan"这样的数据对，拿去**微调一个专门做规划的小模型**（参考仓库的 `training/` 目录做的就是这件事）。

## 本课新增 / 修改的文件

```
app/
├── planner/                    ← 新增目录
│   ├── __init__.py
│   ├── pricing.py              ← 价格 hint
│   ├── attraction_price_table.json  ← 门票价格表（从参考仓库复制）
│   ├── context.py              ← PlannerContextBuilder + compact_for_prompt
│   └── grounding.py            ← 匹配回真实候选
├── agents/
│   ├── generation.py           ← 修改：增加 postprocess 钩子和 warnings
│   ├── context_planner.py      ← 新增
│   ├── prompts.py              ← 追加 build_context_prompt
│   └── deps.py                 ← 修改：增加 context 模式，并设为默认
├── services/amap_client.py     ← 修改：增加限速（Step 2 的踩坑小节）
├── api/routes/trip.py          ← 修改：返回给前端的 message
└── config.py                   ← 修改：planner_mode 增加 "context"，新增 amap_qps_limit
```

```bash
mkdir -p app/planner && touch app/planner/__init__.py
# 从参考仓库复制门票价格表（282 个热门景点，21 个城市，其中北京 12 个）
cp ../helloagents-trip-planner/backend/app/planner/attraction_price_table.json app/planner/
```

> **关于这张价格表**：它来自参考仓库，许可证是 **CC BY-NC-SA 4.0**：使用时要注明出处（BY），不能用于商业用途（NC），修改后再公开发布要用同样的许可证（SA）。个人学习完全没问题，代码里已经写了出处注释。另外，表里的数据是"大模型估算 + 人工审核"的结果（`ticket_price_source` 全是 `llm_estimated`，更新于 2026-05），**不是官方实时票价**，只适合用来估算预算。

## Step 1：价格 hint `app/planner/pricing.py`

```python
"""价格提示（price hint）：给每个候选算好单价，大模型只负责"照抄"

为什么不让大模型自己估价？
  1. 它会"凭感觉"写，同一个景点这次 60、下次 80
  2. 为了凑预算，它会悄悄改价格
有了 hint：价格可追溯（cost_source 说明来源），可复现，后端还能用它覆盖模型的输出
"""

import json
from functools import lru_cache
from pathlib import Path

from app.models.schemas import POIInfo, TripRequest

# 门票价格表：复制自参考仓库 helloagents-trip-planner（github.com/nameless0120/helloagents-trip-planner），
# 许可证 CC BY-NC-SA 4.0（署名、非商业、相同方式共享）
# 数据是"大模型估算 + 人工审核"的结果，不是官方实时票价，只用于预算估算
# Path(__file__).with_name(...)：和当前 .py 文件放在同一个目录下的文件，和第 2 课找 .env 的思路一样
PRICE_TABLE_PATH = Path(__file__).with_name("attraction_price_table.json")

# 住宿偏好 → 每间每晚的参考价（元）。高德没有返回价格时用这个兜底
HOTEL_PRICE_BY_LEVEL = {"经济型酒店": 300, "舒适型酒店": 500, "高端酒店": 900, "民宿": 400}

# 名称或分类里含这些词的景点，通常免费
FREE_ATTRACTION_WORDS = ("公园", "广场", "博物馆", "纪念馆", "步行街", "胡同", "大学")


@lru_cache  # 只在第一次调用时读文件，之后直接返回缓存（和 get_settings 一样的单例技巧）
def load_price_table() -> dict[tuple[str, str], dict]:
    """读取票价表，整理成 {(城市, 景点名): 价格画像} 的字典，查表时直接按 key 取，不用逐条遍历"""
    data = json.loads(PRICE_TABLE_PATH.read_text(encoding="utf-8"))
    table: dict[tuple[str, str], dict] = {}
    for item in data["items"]:
        # 正式名和别名都登记一份，例如 ("北京", "故宫博物院") 和 ("北京", "故宫") 指向同一条
        # [item["name"], *item["aliases"]]：* 把列表展开，相当于 JS 的 [item.name, ...item.aliases]
        for name in [item["name"], *item["aliases"]]:
            table[(item["city"], name)] = item["ticket_price_profile"]
    return table


def ticket_season(request: TripRequest) -> str:
    """按出发月份粗分票价季节：11-3 月淡季，7-8 月旺季，其余平季（不考虑节假日）"""
    month = request.start_date.month
    if month in (11, 12, 1, 2, 3):
        return "off_season"
    if month in (7, 8):
        return "peak_season"
    return "normal_season"


def ticket_price_hint(poi: POIInfo, request: TripRequest) -> tuple[int, str]:
    """返回 (成人票价, 价格来源)：先查价格表，查不到再走关键词规则"""
    # removesuffix：去掉末尾的"市"，"北京市" → "北京"，和表里的城市名对齐
    profile = load_price_table().get((request.city.removesuffix("市"), poi.name))
    if profile:
        # 例如故宫：{"off_season": 40, "normal_season": 60, "peak_season": 60}
        return int(profile[ticket_season(request)]), "price_table"

    # 查表优先于规则：天坛公园名字里有"公园"，按规则会判成免费，但表里是 10 元
    text = poi.name + poi.type
    if any(word in text for word in FREE_ATTRACTION_WORDS):  # any：只要有一个满足就为 True
        return 0, "rule_free"
    return 60, "default"


def meal_cost_hint(poi: POIInfo) -> tuple[int, str]:
    """返回 (人均价格, 价格来源)：优先用高德的人均消费"""
    if poi.cost and poi.cost > 0:
        return round(poi.cost), "amap_cost"
    return 60, "default"


def hotel_cost_hint(poi: POIInfo, request: TripRequest) -> tuple[int, str]:
    """返回 (每间每晚价格, 价格来源)"""
    if poi.cost and poi.cost > 0:
        return round(poi.cost), "amap_cost"
    # dict.get(key, 默认值)：找不到住宿类型时按 400 算
    return HOTEL_PRICE_BY_LEVEL.get(request.accommodation, 400), "level_default"
```

### 价格表的结构和查表逻辑

表里的一条数据长这样（只保留了用到的字段）：

```json
{"city": "北京", "name": "故宫博物院", "aliases": ["故宫"],
 "ticket_price_profile": {"off_season": 40, "normal_season": 60, "peak_season": 60}}
```

`load_price_table` 把它整理成以 `(城市, 名字)` 为 key 的字典，正式名和别名各登记一份：

```
("北京", "故宫博物院") ─┐
("北京", "故宫")       ─┴─→ {"off_season": 40, "normal_season": 60, "peak_season": 60}
```

查表时用 `(城市, 景点名)` 直接取，取到了就按出发月份选季节价格。实测结果：

| 景点 | 城市 | 出发日期 | 结果 | 说明 |
|---|---|---|---|---|
| 故宫博物院 | 北京 | 2026-10-06 | `(60, "price_table")` | 平季 |
| 故宫 | 北京 | 2026-12-01 | `(40, "price_table")` | 别名 + 淡季 |
| 天坛公园 | 北京 | 2026-10-06 | `(10, "price_table")` | **名字里有"公园"，按规则会判成免费，查表优先，所以是 10 元** |
| 景山公园 | 北京 | 2026-10-06 | `(0, "rule_free")` | 表里没有，退回规则 |
| 故宫博物院 | 上海 | 2026-10-06 | `(60, "default")` | 城市不同，不会误匹配 |

**为什么用 `(城市, 名字)` 当 key，而不是只用名字？** 不同城市可能有同名景点（"人民公园"几乎每个城市都有）。tuple 可以当 dict 的 key，list 不行，因为 key 必须是不可修改的。

> 参考仓库的查表逻辑更复杂：自动生成别名（"锦里古街" → "锦里"）、城市名归一化（"大理" ↔ "大理白族自治州"）、按相似度打分匹配。我们先用最简单的"精确匹配 + 表里自带的别名"，遇到匹配不上的情况再改进（见作业 2）。

## Step 2：上下文构建 `app/planner/context.py`

```python
"""PlannerContext：把规划需要的所有信息，整理成一份结构化、可审计的"案卷"再交给大模型

和第 8 课多 Agent 版的对比：
  多 Agent：大模型决定调用什么工具 → 结果被总结成自然语言 → 工具出错也没人知道
  Context：代码决定调用什么工具（并行执行）→ 保留结构化数据 → 每个工具的成败都记在 tool_status 里
"""

import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable

from app.agents.output import trip_dates
from app.models.schemas import POIInfo, TripRequest, WeatherInfo
from app.planner.pricing import hotel_cost_hint, meal_cost_hint, ticket_price_hint
from app.services.amap_client import AmapClient


def poi_row(poi: POIInfo, **hints: Any) -> dict[str, Any]:
    """POI → 给大模型看的精简 dict，**hints 用来附加价格提示等字段（相当于 JS 的 ...rest）"""
    return {
        "name": poi.name,
        "district": poi.district,
        "address": poi.address[:40],  # 截断过长的地址，节省 token
        "location": poi.location.model_dump(),
        "rating": poi.rating,
        **hints,
    }


def align_weather(request: TripRequest, forecasts: list[WeatherInfo]) -> list[dict[str, Any]]:
    """把高德的预报（只有今天起 4 天）对齐到行程的每一天，没有预报的日期明确标注出来（第 5 课作业 2）"""
    by_date = {w.date: w for w in forecasts}  # 字典推导式：日期 → 天气
    rows = []
    for date in trip_dates(request):
        if date in by_date:
            rows.append(by_date[date].model_dump() | {"source": "amap_forecast"})  # | 合并两个字典
        else:
            rows.append({"date": date, "day_weather": "暂无预报", "day_temp": "未知", "night_temp": "未知", "source": "unavailable"})
    return rows


class PlannerContextBuilder:
    def __init__(self, amap: AmapClient):
        self.amap = amap

    # ---------- 每个工具一个方法，返回 {字段名: 数据} ----------

    def _collect_attractions(self, request: TripRequest) -> dict[str, Any]:
        keywords = request.preferences[:3] or ["景点"]
        pois: dict[str, POIInfo] = {}
        for keyword in keywords:
            for poi in self.amap.search_poi(keyword, request.city, limit=10):
                pois.setdefault(poi.id, poi)  # setdefault：key 不存在才写入，实现按 id 去重并保留先出现的
        rows = []
        for poi in pois.values():
            price, source = ticket_price_hint(poi, request)  # 查表要知道城市和出发日期，所以多传一个 request
            rows.append(poi_row(poi, ticket_price_hint=price, price_source=source))
        return {"attraction_pois": rows}

    def _collect_hotels(self, request: TripRequest) -> dict[str, Any]:
        rows = []
        for poi in self.amap.search_poi(request.accommodation, request.city, limit=8):
            price, source = hotel_cost_hint(poi, request)
            rows.append(poi_row(poi, estimated_cost_hint=price, price_source=source))
        return {"hotel_pois": rows}

    def _collect_foods(self, request: TripRequest) -> dict[str, Any]:
        rows = []
        for poi in self.amap.search_poi("美食", request.city, limit=20):
            price, source = meal_cost_hint(poi)
            rows.append(poi_row(poi, meal_cost_hint=price, price_source=source))
        return {"food_pois": rows}

    def _collect_weather(self, request: TripRequest) -> dict[str, Any]:
        return {"trip_weather": align_weather(request, self.amap.get_weather(request.city))}

    # ---------- 并行执行所有工具 ----------

    def collect(self, request: TripRequest) -> dict[str, Any]:
        jobs: dict[str, Callable[[TripRequest], dict[str, Any]]] = {
            "attractions": self._collect_attractions,
            "hotels": self._collect_hotels,
            "foods": self._collect_foods,
            "weather": self._collect_weather,
        }
        # 先放好空值：即使某个工具失败，context 的结构也是完整的（下游代码不用判断 key 是否存在）
        snapshot: dict[str, Any] = {
            "attraction_pois": [],
            "hotel_pois": [],
            "food_pois": [],
            "trip_weather": align_weather(request, []),
        }
        tool_status: dict[str, dict[str, Any]] = {}

        # 线程池：4 个工具同时调高德，总耗时 ≈ 最慢的那一个，而不是 4 个相加
        # 相当于 JS 的 Promise.allSettled（每个任务单独成功或失败，互不影响）
        with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
            futures = {name: pool.submit(func, request) for name, func in jobs.items()}
            for name, future in futures.items():
                try:
                    result = future.result()  # 等待这个任务完成；任务里抛出的异常会在这里重新抛出
                    snapshot.update(result)
                    count = len(next(iter(result.values())))  # 取出结果里唯一的那个列表，统计条数
                    tool_status[name] = {"ok": True, "count": count}
                except Exception as exc:
                    # 关键：失败被明确记录下来，而不是像多 Agent 版那样变成一段自然语言
                    tool_status[name] = {"ok": False, "error": str(exc)}
                    print(f"⚠️  工具 {name} 失败: {exc}")

        party = request.party
        return {
            "version": "planner_context_v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "request": request.model_dump(mode="json"),  # mode="json"：date 等类型转成字符串，保证能 json.dumps
            "party": {"total": party.total, "rooms": math.ceil(party.total / 2)},
            "tool_snapshot": snapshot,
            "tool_status": tool_status,
            "planner_constraints": {
                "expected_dates": trip_dates(request),
                "attractions_per_day": "1-3",
                "meals_per_day": ["breakfast", "lunch", "dinner"],
                "pricing_rule": (
                    "ticket_price 必须等于所选景点的 ticket_price_hint；"
                    "hotel.estimated_cost 必须等于 estimated_cost_hint；"
                    "lunch/dinner 的 estimated_cost 必须等于所选餐厅的 meal_cost_hint"
                ),
            },
        }


def compact_for_prompt(context: dict[str, Any]) -> dict[str, Any]:
    """给大模型看的版本：去掉 generated_at、tool_status、price_source 等只给人和代码看的字段"""
    snapshot = context["tool_snapshot"]

    def strip(rows: list[dict]) -> list[dict]:
        return [{k: v for k, v in row.items() if k != "price_source"} for row in rows]

    return {
        "request": context["request"],
        "party": context["party"],
        "tool_snapshot": {
            "attraction_pois": strip(snapshot["attraction_pois"]),
            "hotel_pois": strip(snapshot["hotel_pois"]),
            "food_pois": strip(snapshot["food_pois"]),
            "trip_weather": [{k: v for k, v in w.items() if k != "source"} for w in snapshot["trip_weather"]],
        },
        "planner_constraints": context["planner_constraints"],
    }
```

> **为什么要有 compact 这一步？** 完整的 context 留在后端，用于日志、调试和 grounding；给模型看的版本要尽量精简。参考仓库会在日志里打印 `PlannerContext压缩: 原始长度 -> 压缩后长度 字符`（见 `agents/planner_query.py`），压缩省下的都是实打实的费用和响应时间。

### 踩坑：并行调用撞上高德的 QPS 限制

`collect` 用线程池同时调用 4 个工具，一瞬间会发出 5、6 个高德请求。用真实 Key 测试时，很容易遇到这样的日志：

```
⚠️  工具 attractions 失败: 高德返回错误: CUQPS_HAS_EXCEEDED_THE_LIMIT（infocode=10021）
```

`CUQPS_HAS_EXCEEDED_THE_LIMIT` 的意思是**每秒请求数超过了 Key 的上限**，个人开发者 Key 的上限比较低（参考仓库默认按每秒 3 次处理）。第 6、7 课的直连版是一个接一个调用的，所以没遇到；**并行让速度变快了，也更容易撞上限流。**

> 这时第 9 课的防护其实已经在工作了：失败被记录在 `tool_status` 里，景点数据缺失时直接返回兜底方案，没有让大模型凭空编造。但每次都兜底显然不行，要从源头解决。

解决办法是**限速**：所有高德请求都经过 `AmapClient._get`，在这个入口处让请求排队，保证两次请求之间至少间隔 1/3 秒。参考仓库 `planner/amap.py` 的 `_wait_for_amap_slot` 也是这么做的。

修改第 5 课的 `app/services/amap_client.py`。文件顶部的导入区追加：

```python
import threading
import time
```

`AmapClient` 的 `__init__` 整个替换，并新增 `_wait_for_slot` 方法：

```python
    def __init__(self, api_key: str, transport: httpx.BaseTransport | None = None, qps_limit: float = 3):
        """
        api_key：高德 Web 服务 Key
        transport：只在测试时传入，用来模拟高德返回的数据，不发真实请求（第 11 课会用到）
        qps_limit：每秒最多发几个请求（第 9 课），0 表示不限速（测试时用）
        """
        if not api_key:
            raise AmapError("AMAP_API_KEY 未配置，请在 backend/.env 中填写")
        self.api_key = api_key
        # httpx.Client 相当于 axios.create({...})：复用连接，统一 baseURL 和超时
        self.http = httpx.Client(base_url=AMAP_BASE_URL, timeout=10, transport=transport)

        # ---------- 限速（第 9 课）----------
        # 两次请求之间至少间隔多少秒：每秒 3 次 → 间隔 1/3 秒
        self.min_interval = 1 / qps_limit if qps_limit > 0 else 0
        # 线程锁：第 9 课用线程池并行调高德，锁保证同一时刻只有一个线程在"检查间隔、更新时间"
        self._lock = threading.Lock()
        self._last_request_at = 0.0  # 上一次请求发出的时间

    def _wait_for_slot(self) -> None:
        """排队：距离上一次请求不足 min_interval 秒，就先睡一会儿再发"""
        if self.min_interval <= 0:
            return
        with self._lock:  # 拿到锁才能往下执行，其他线程在这里排队等待
            # time.monotonic()：只增不减的时钟，专门用来算时间间隔（相当于前端的 performance.now()）
            wait = self.min_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
```

`_get` 方法的**第一行**加上排队：

```python
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """所有请求的统一入口：限速、带上 key、处理网络错误、处理高德业务错误"""
        self._wait_for_slot()  # 第 9 课：所有高德请求都经过这里，在入口处统一排队
        try:
            ...（后面不变）
```

文件末尾的 `get_amap_client` 改成从配置读取限速：

```python
@lru_cache
def get_amap_client() -> AmapClient:
    settings = get_settings()
    return AmapClient(settings.amap_api_key, qps_limit=settings.amap_qps_limit)
```

`app/config.py` 的 `amap_api_key` 下面追加一行（`.env.example` 里也补上 `AMAP_QPS_LIMIT=3`）：

```python
    amap_qps_limit: float = 3  # 高德每秒最多请求几次，个人开发者 Key 的并发上限比较低；0 = 不限速
```

**为什么限速状态可以放在实例上？** `get_amap_client` 用 `@lru_cache` 做成了单例，整个进程只有一个 `AmapClient`，所有线程共用同一把锁和同一个"上次请求时间"。

**为什么需要锁？** 4 个线程可能同时走进 `_wait_for_slot`。没有锁的话，它们可能同时读到"距离上次请求已经超过 1/3 秒"，然后一起把请求发出去，限速就失效了。前端很少碰到这个问题，因为 JS 是单线程的。

**实测**：写了一个模拟 QPS 限制的假高德（1 秒内超过 3 个请求就返回 `CUQPS_HAS_EXCEEDED_THE_LIMIT`），同一个请求：

| | 结果 |
|---|---|
| `qps_limit=0`（不限速） | 景点、酒店都被拒绝，只有餐厅和天气成功 |
| `qps_limit=3` | 4 个工具全部成功；6 个请求的间隔都是 0.33~0.34 秒，收集耗时约 1.7 秒 |

代价是收集数据多花 1 秒多，相比大模型生成要几十秒，可以接受。

> **注意**：限速只在**一个进程内**有效。第 11 课提到生产环境用 `--workers 4` 启动 4 个进程，每个进程各有一个 `AmapClient`，合起来就是每秒 12 次，又会超限。多进程要么把 `AMAP_QPS_LIMIT` 按进程数分摊，要么用 Redis 这类共享存储做全局限速。

## Step 3：Grounding `app/planner/grounding.py`

```python
"""Grounding（接地）：检查大模型选的景点、酒店、餐厅是不是真的来自候选数据

做两件事：
  1. 找到对应的候选 → 用候选的真实数据覆盖模型写的坐标、地址、价格（模型抄错了也能纠正回来）
  2. 找不到 → 记下来，算出 grounding 率，作为质量指标
"""

import re
from typing import Any

from app.models.schemas import Location, TripPlan


def normalize_name(name: str) -> str:
    """去掉空格和括号里的分店信息，例如 "全聚德(前门店)" → "全聚德"，提高匹配成功率"""
    name = re.sub(r"[（(].*?[)）]", "", name)  # 正则：删除中英文括号及其内容
    return re.sub(r"\s+", "", name).lower()


def find_candidate(name: str, rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """按名称查找候选：先精确匹配，再看两者是否互相包含"""
    target = normalize_name(name)
    if not target:
        return None
    for row in rows:
        if normalize_name(row["name"]) == target:
            return row
    for row in rows:
        candidate = normalize_name(row["name"])
        if target in candidate or candidate in target:
            return row
    return None


def ground_plan(plan: TripPlan, context: dict[str, Any]) -> dict[str, Any]:
    """原地修正 plan，返回统计信息 {"attraction_rate": 0.9, "ungrounded": [...]}"""
    snapshot = context["tool_snapshot"]
    ungrounded: list[str] = []
    total = matched = 0

    for day in plan.days:
        for attraction in day.attractions:
            total += 1
            row = find_candidate(attraction.name, snapshot["attraction_pois"])
            if row is None:
                ungrounded.append(f"景点:{attraction.name}")
                continue
            matched += 1
            attraction.location = Location(**row["location"])  # **dict 展开成关键字参数
            attraction.address = attraction.address or row["address"]
            attraction.ticket_price = row["ticket_price_hint"]  # 价格以 hint 为准，模型改价格也没用

        if day.hotel:
            row = find_candidate(day.hotel.name, snapshot["hotel_pois"])
            if row is None:
                ungrounded.append(f"酒店:{day.hotel.name}")
            else:
                day.hotel.location = Location(**row["location"])
                day.hotel.estimated_cost = row["estimated_cost_hint"]

        for meal in day.meals:
            if meal.type not in ("lunch", "dinner"):
                continue  # 早餐可能是酒店早餐，不强求来自候选
            row = find_candidate(meal.name, snapshot["food_pois"])
            if row is None:
                ungrounded.append(f"餐厅:{meal.name}")
            else:
                meal.estimated_cost = row["meal_cost_hint"]

    rate = matched / total if total else 0.0
    return {"attraction_rate": round(rate, 2), "ungrounded": ungrounded}
```

## Step 4：生成循环加钩子 `app/agents/generation.py`（整个文件替换）

```python
"""生成 + 校验 + 重试 + 兜底（第 9 课：增加 postprocess 钩子和 warnings）"""

from dataclasses import dataclass, field
from typing import Callable

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.output import parse_and_check, recompute_budget
from app.agents.prompts import PLANNER_SYSTEM_PROMPT
from app.models.schemas import TripPlan, TripRequest
from app.services.mock_planner import build_mock_plan

MAX_ATTEMPTS = 3
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
) -> PlanResult:
    errors: list[str] = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        prompt = base_prompt
        if errors:
            prompt += f"\n\n注意：上一次的输出不合格，原因是：{errors[-1]}。请修正后重新输出完整 JSON。"
        temperature = BASE_TEMPERATURE + 0.1 * (attempt - 1)
        messages = [SystemMessage(PLANNER_SYSTEM_PROMPT), HumanMessage(prompt)]
        try:
            response = llm.invoke(messages, temperature=temperature).text
            plan = parse_and_check(response, request)
            warnings = postprocess(plan) if postprocess else []
            # 后处理可能修改了价格（grounding 用 hint 覆盖），所以预算要重新算一次
            plan.budget = recompute_budget(plan, request)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc).splitlines()[0]}"
            errors.append(reason)
            print(f"⚠️  第 {attempt}/{MAX_ATTEMPTS} 次失败: {reason}")
            continue
        print(f"✅ 第 {attempt} 次生成成功")
        return PlanResult(plan=plan, source="llm", attempts=attempt, errors=errors, warnings=warnings)

    print("❌ 大模型多次生成失败，返回兜底方案")
    return PlanResult(
        plan=build_fallback(request, "大模型多次生成失败"),
        source="fallback",
        attempts=MAX_ATTEMPTS,
        errors=errors,
    )
```

## Step 5：提示词 `app/agents/prompts.py` 末尾追加

```python
# ============================================================
# 第 9 课：PlannerContext 版的 User Prompt
# ============================================================

def build_context_prompt(context_json: str, days: int) -> str:
    return f"""请根据下面的 PlannerContext 生成 {days} 天的旅行计划。

PlannerContext:
{context_json}

规则：
1. 景点只能从 tool_snapshot.attraction_pois 里选，酒店只能从 hotel_pois 里选，午餐和晚餐只能从 food_pois 里选；name 必须原样复制。
2. 价格严格按照 planner_constraints.pricing_rule 复制 hint，不要自己估价，也不要为了凑预算修改价格。
3. days 的数量和日期必须与 planner_constraints.expected_dates 完全一致。
4. weather_info 逐日复制 tool_snapshot.trip_weather，不要编造天气。
5. 同一天的景点尽量在同一个 district；同一天的午餐和晚餐不要选同一家。
6. 只输出 JSON。"""
```

## Step 6：规划器 `app/agents/context_planner.py`

```python
"""PlannerContext 版规划器（第 9 课，参考仓库的现行架构）

TripRequest
  → PlannerContextBuilder.collect：并行调高德，附上价格 hint，记录每个工具的成败
  → 关键数据缺失？→ 直接返回兜底方案，不浪费大模型调用
  → compact_for_prompt：精简后交给规划 Agent（只调用 1 次大模型，失败才重试）
  → parse_and_check：结构和业务校验（第 7 课）
  → ground_plan：匹配回真实候选，用真实数据覆盖坐标和价格
  → recompute_budget：按统一口径重算预算
"""

import json

from langchain_core.language_models import BaseChatModel

from app.agents.generation import PlanResult, build_fallback, generate_plan_with_retries
from app.agents.prompts import build_context_prompt
from app.models.schemas import TripPlan, TripRequest, WeatherInfo
from app.planner.context import PlannerContextBuilder, compact_for_prompt
from app.planner.grounding import ground_plan
from app.services.amap_client import AmapClient


class ContextTripPlanner:
    def __init__(self, amap: AmapClient, llm: BaseChatModel):
        self.builder = PlannerContextBuilder(amap)
        self.llm = llm

    def plan(self, request: TripRequest) -> PlanResult:
        context = self.builder.collect(request)
        status = context["tool_status"]
        print(f"🧰 工具状态: {status}")

        # 景点是行程的核心：没有景点候选，就不让大模型"凭空发挥"
        # 对比第 8 课：那时工具全部失败，大模型照样编出了一份行程
        if not context["tool_snapshot"]["attraction_pois"]:
            reason = f"景点数据获取失败: {status.get('attractions', {}).get('error', '没有搜到景点')}"
            return PlanResult(plan=build_fallback(request, reason), source="fallback", attempts=0, errors=[reason])

        prompt_context = compact_for_prompt(context)
        # separators=(",", ":")：去掉 JSON 里多余的空格，又能省一些 token
        context_json = json.dumps(prompt_context, ensure_ascii=False, separators=(",", ":"))
        print(f"📦 PlannerContext: {len(json.dumps(context, ensure_ascii=False))} → 精简后 {len(context_json)} 字符")

        def postprocess(plan: TripPlan) -> list[str]:
            """闭包：内部函数可以直接访问外层的 context 变量"""
            stats = ground_plan(plan, context)
            print(f"📍 Grounding: 景点命中率 {stats['attraction_rate']:.0%}，未匹配 {stats['ungrounded']}")

            # 天气完全由代码决定：直接用对齐好的 trip_weather 覆盖，模型漏抄、编造都不影响
            plan.weather_info = [WeatherInfo(**w) for w in context["tool_snapshot"]["trip_weather"]]

            # 工具失败排在前面：比"某家餐厅不在候选里"重要得多
            warnings = [f"{name} 数据获取失败" for name, s in status.items() if not s["ok"]]
            # dict.fromkeys 去重并保持原有顺序（同一家餐厅可能被选了好几次）
            warnings += [f"{name} 不在高德候选中" for name in dict.fromkeys(stats["ungrounded"])]
            return warnings

        prompt = build_context_prompt(context_json, request.travel_days)
        return generate_plan_with_retries(self.llm, prompt, request, postprocess=postprocess)
```

## Step 7：配置和依赖

`app/config.py`：

```python
    planner_mode: Literal["context", "direct", "multi_agent"] = "context"
```

`app/agents/deps.py` 的 `get_trip_planner`，在判断 `multi_agent` 之前加上：

```python
    mode = get_settings().planner_mode
    if mode == "context":  # 第 9 课起的默认模式
        from app.agents.context_planner import ContextTripPlanner

        return ContextTripPlanner(amap=get_amap_client(), llm=llm)

    if mode == "multi_agent":
        ...
```

`app/api/routes/trip.py`（整个文件替换）：

```python
"""旅行规划相关接口"""

from fastapi import APIRouter, Depends

from app.agents.deps import Planner, get_trip_planner
from app.agents.output import recompute_budget
from app.config import get_settings
from app.models.schemas import TripPlanResponse, TripRequest
from app.services.mock_planner import build_mock_plan

router = APIRouter(prefix="/trip", tags=["旅行规划"])


@router.get("/health", summary="旅行规划健康检查")
def trip_health():
    settings = get_settings()
    return {
        "status": "healthy",
        "service": "trip-planner",
        "amap_configured": bool(settings.amap_api_key),
        "llm_configured": bool(settings.llm_api_key),
    }


# def 而不是 async def：Agent 内部的高德调用和大模型调用都是同步阻塞的（见第 5 课的表格）
@router.post("/plan", response_model=TripPlanResponse, summary="生成旅行计划")
def plan_trip(
    request: TripRequest,
    # 依赖注入：没配大模型时拿到的是 None
    planner: Planner | None = Depends(get_trip_planner),
):
    if planner is None:
        plan = build_mock_plan(request)
        plan.budget = recompute_budget(plan, request)  # 第 9 课：mock 数据也按统一口径计算预算（酒店要乘房间数）
        return TripPlanResponse(success=True, message="未配置大模型，返回 mock 数据", data=plan)

    # 第 7 课：plan() 内部已经处理了重试和兜底，这里不会再因为大模型输出格式问题抛异常
    result = planner.plan(request)
    if result.source == "fallback":
        reason = result.errors[-1] if result.errors else "未知原因"
        message = f"智能规划失败，返回基础行程。原因：{reason}"
    else:
        message = f"旅行计划生成成功（第 {result.attempts} 次生成）"
    if result.warnings:
        message += "；注意：" + "；".join(result.warnings[:3])  # 最多显示 3 条，避免提示过长
    return TripPlanResponse(success=True, message=message, data=result.plan)
```

## Step 8：验证

```bash
# .env 里删掉 PLANNER_MODE，或者改成 context
uv run python run.py
```

提交一次，后端日志大致如下：

```
🧰 工具状态: {'attractions': {'ok': True, 'count': 条数}, 'hotels': {...}, ...}
📦 PlannerContext: 原始长度 → 精简后长度 字符
✅ 第 1 次生成成功
📍 Grounding: 景点命中率 100%，未匹配 ['餐厅:xxx']
```

**和第 8 课做对比**：故意把 `.env` 里的高德 Key 改错，再提交一次。第 8 课会"成功"返回一份编出来的行程；现在你会直接拿到兜底方案，并看到明确的失败原因，而且大模型一次都没被调用。

## 和参考仓库的差距（进阶方向）

我们实现的是参考仓库的骨架。参考仓库在这个骨架上还做了很多打磨，读源码时可以对照着看：

| 参考仓库的能力 | 位置 | 解决的问题 |
|---|---|---|
| 门票价格表的模糊匹配：自动生成别名、城市名归一化、相似度打分（表本身我们已经用上了） | `planner/pricing.py` 的 `find_attraction_price_profile`、`poi_name_aliases` | 高德返回的名字和表里的写法不完全一样时匹配不上 |
| 按偏好、预算生成多组关键词 | `planner/pois.py` | 搜索召回的景点不够丰富 |
| 餐饮按早 / 午 / 晚、菜系、忌口打标签 | `planner/pois.py` 的 `annotate_food_pois` | 三餐雷同、踩到忌口 |
| 高预算时补充高端候选 | `planner/high_end_candidates.py` | 预算 2 万却只推荐经济型 |
| 本地缓存（限速我们已经做了） | `planner/amap.py` 的 `_read_cache` / `_write_cache` | 高德有每日调用量限制，同样的搜索没必要重复请求 |
| 生成多个候选再打分择优（rerank） | `planner/rerank.py` | 单次生成的质量不稳定 |

## 📝 课后作业

- [ ] 1. **核心问题**：用你自己的话，结合本课的对比表，写一段 300 字左右的说明："为什么参考仓库要从多 Agent 改成 PlannerContext？"（这是整门课最重要的一份作业）
- [ ] 2. **改进查表的匹配**：现在是精确匹配，有两种情况会查不到：① 用户输入"大理"，表里写的是"大理白族自治州"；② 高德返回"故宫博物院(午门)"这种带括号的名字。先写两个能复现问题的例子，确认确实查不到，再改进 `ticket_price_hint`。（提示：第 ② 种可以复用 Step 3 `grounding.py` 里的 `normalize_name`；改完之后，想想为什么 `load_price_table` 里登记 key 时也要做同样的处理）
- [ ] 3. **grounding 的硬性门槛**：现在景点不在候选里只会产生警告。改成景点命中率低于 50% 时抛出 `PlanValidationError`，触发重试。想一想：门槛定得太高会怎样？太低又会怎样？
- [ ] 4. **保存 context**：每次生成后，把 `{context, plan, grounding 统计}` 存成 `backend/logs/contexts/时间戳.json`。存够 10 份之后翻一翻，看看能发现模型哪些规律性的问题。

**提交方式**：作业 1 的文字、作业 2、3 的代码发给老师。下一课：结果页升级（地图、编辑、导出）。

## 我的笔记

<!-- 在这里记录你的答案和心得 -->
