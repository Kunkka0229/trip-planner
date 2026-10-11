# 第 3 课：Pydantic 数据模型

**目标**：定义整个项目的"数据契约"：前端发来什么（`TripRequest`），后端返回什么（`TripPlan`）。然后用 **mock 数据**实现 `POST /api/trip/plan`，让前端（第 4 课）在没有大模型的情况下也能先联调。

对应：书 13.2，参考仓库 `backend/app/models/schemas.py`。

## 为什么需要数据模型？

第 1、2 课的接口直接返回 dict，有三个问题：

1. **没有校验**：前端传 `{"adults": -3}` 也照单全收。
2. **没有文档**：`/docs` 里看不出接口要传什么、会返回什么。
3. **字段容易写错**：`"citiy"` 拼错了，不运行根本发现不了。

Pydantic 模型同时解决这三个问题。前端类比：**它相当于 TypeScript 的 `interface` + zod 校验 + Swagger 文档，三合一**。TS 类型只在编译期存在，运行时就没了；Pydantic 的类型在**运行时真的会校验数据**。

## 本课结束后的目录（新增部分）

```
app/
├── models/
│   ├── __init__.py
│   └── schemas.py          ← 所有数据模型
├── services/
│   ├── __init__.py
│   └── mock_planner.py     ← 根据请求生成假行程（第 6 课换成大模型）
└── api/routes/trip.py      ← 新增 POST /plan
```

```bash
cd /Users/star/Desktop/star/agent/my-helloagents-trip-planner/backend
mkdir -p app/models app/services
touch app/models/__init__.py app/services/__init__.py
```

## Pydantic 速查（先看一遍，写代码时再回来对照）

| 写法 | 含义 | TS / zod 类比 |
|---|---|---|
| `name: str` | 必填字符串 | `name: string` |
| `name: str = ""` | 可选，有默认值 | `name?: string`（默认 `""`） |
| `amount: int \| None = None` | 可以是 null | `amount: number \| null` |
| `tags: list[str] = Field(default_factory=list)` | 默认空列表 | `tags: string[] = []` |
| `Field(..., ge=1, le=20)` | `...` 表示必填；`ge`/`le` 是 ≥ / ≤ | `z.number().min(1).max(20)` |
| `Field(min_length=1)` | 字符串最短长度 | `z.string().min(1)` |
| `Literal["a", "b"]` | 只能取这几个值 | `'a' \| 'b'` |
| `party: PartyInfo` | 嵌套另一个模型 | 嵌套 interface |
| `@field_validator` | 校验或转换**单个字段** | `z.string().transform(...)` |
| `@model_validator` | 校验**多个字段之间的关系** | `z.object().refine(...)` |
| `@computed_field` | 根据其他字段算出来的只读字段 | `get total()` |
| `Model.model_validate(dict)` | dict → 模型（带校验） | `schema.parse(obj)` |
| `obj.model_dump()` | 模型 → dict | `JSON.parse(JSON.stringify(obj))` |

> **为什么 `tags` 要用 `default_factory=list`，而不是 `tags: list[str] = []`？**
> Pydantic v2 对 `= []` 其实也做了安全处理，但在普通 Python 函数和类里，`= []` 会让所有实例**共享同一个列表**，这是一个经典坑。统一用 `default_factory`，养成好习惯。

## Step 1：`app/models/schemas.py`

按书里"**自底向上**"的思路：先定义最小的积木（人数、预算、坐标），再拼成大模型。代码比较长，**建议分三段敲，每敲完一段就在 `/docs` 里看看效果**。

### 第一段：请求模型（前端 → 后端）

```python
"""数据模型：前后端之间的"数据契约" """

from datetime import date  # Python 的日期类型（只有年月日，没有时分秒）
from typing import Literal  # 用来限定字段只能取几个固定值

from pydantic import (
    BaseModel,         # 所有模型的基类，相当于"这是一个 interface，并且运行时会校验"
    ConfigDict,        # 模型级别的配置
    Field,             # 给字段加约束：默认值、范围、描述……
    computed_field,    # 计算字段
    field_validator,   # 单字段校验器
    model_validator,   # 整个模型的校验器
)


# ============================================================
# 请求模型：前端提交的旅行需求
# ============================================================

class PartyInfo(BaseModel):
    """同行人员"""

    # Field(...) 里的 ... 叫 Ellipsis，在 Pydantic 里表示"必填，没有默认值"
    # ge = greater than or equal（≥），le = less than or equal（≤）
    # description 会显示在 /docs 里，相当于字段注释
    adults: int = Field(..., ge=1, le=20, description="成人数量，至少 1 人")
    children: int = Field(default=0, ge=0, le=20, description="儿童数量")
    elders: int = Field(default=0, ge=0, le=20, description="老人数量")

    # 计算字段：总人数根据前三项算出来，不需要前端传
    # （参考仓库要求前端传 total，再校验它是否等于三项之和；我们直接算出来，前端少传一个字段，也就不可能传错）
    # @computed_field + @property：输出 JSON 时会带上 total，在 /docs 的响应结构里也能看到
    @computed_field
    @property
    def total(self) -> int:
        return self.adults + self.children + self.elders


class BudgetConstraint(BaseModel):
    """预算约束"""

    # int | None：可以是整数，也可以是 null（用户没填预算）
    # 相当于 TS 的 amount: number | null
    amount: int | None = Field(default=None, ge=0, description="整趟旅行总预算（元），不限预算时为 null")

    # Literal：只能是这三个字符串之一，传其他值直接返回 422
    # 相当于 TS 的 'hard' | 'soft' | 'none'
    # hard = 绝对不能超；soft = 尽量不超；none = 不限制
    strictness: Literal["hard", "soft", "none"] = Field(default="none", description="预算严格程度")


class TripRequest(BaseModel):
    """旅行规划请求：POST /api/trip/plan 的请求体"""

    # json_schema_extra：给 /docs 提供一份示例请求体，点 "Try it out" 时会自动填好
    # （参考仓库用的 class Config: 是 Pydantic v1 的旧写法，v2 推荐 model_config = ConfigDict(...)）
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "city": "北京",
                    "start_date": "2026-10-01",
                    "end_date": "2026-10-03",
                    "travel_days": 3,
                    "transportation": "公共交通",
                    "accommodation": "经济型酒店",
                    "preferences": ["历史文化", "美食"],
                    "free_text_input": "希望多安排一些博物馆",
                    "party": {"adults": 2, "children": 1, "elders": 0},
                    "budget_constraint": {"amount": 3000, "strictness": "soft"},
                }
            ]
        }
    )

    city: str = Field(..., min_length=1, max_length=20, description="目的地城市")

    # 类型写成 date，而不是 str（参考仓库用的是 str）：
    # 前端传 "2026-10-01"，Pydantic 会自动解析成 date 对象；
    # 传 "2026-13-01" 这种不存在的日期，直接返回 422，业务代码里不用再手写日期校验
    start_date: date = Field(..., description="开始日期 YYYY-MM-DD")
    end_date: date = Field(..., description="结束日期 YYYY-MM-DD")
    travel_days: int = Field(..., ge=1, le=15, description="旅行天数")

    transportation: str = Field(default="公共交通", description="交通方式")
    accommodation: str = Field(default="经济型酒店", description="住宿偏好")
    preferences: list[str] = Field(default_factory=list, description="偏好标签")
    free_text_input: str = Field(default="", description="额外要求")

    # 嵌套模型：party 必须是一个符合 PartyInfo 结构的对象，里面的字段同样会被校验
    party: PartyInfo
    # 预算可以不传，不传时用 BudgetConstraint() 创建默认值（amount=None, strictness="none"）
    budget_constraint: BudgetConstraint = Field(default_factory=BudgetConstraint)

    # ---------- 单字段校验器 ----------
    # 只作用于 city 字段，在类型校验之后执行（默认 mode="after"）
    # @classmethod 是 Pydantic 的要求：校验器写成"类方法"，第一个参数是类本身 cls
    @field_validator("city")
    @classmethod
    def strip_city(cls, v: str) -> str:
        v = v.strip()  # 去掉首尾空格，相当于 JS 的 trim()
        if not v:
            # 抛出 ValueError，Pydantic 会把它转成 422 错误返回给前端
            raise ValueError("城市不能为空")
        return v  # 必须返回值，返回什么，字段最终就是什么（可以借此做"清洗"）

    # ---------- 模型校验器 ----------
    # mode="after"：所有字段都校验并转换完之后再执行，这时 self.start_date 已经是 date 对象了
    # 适合校验"字段之间的关系"，这是单字段校验器做不到的
    @model_validator(mode="after")
    def check_dates(self):
        if self.end_date < self.start_date:  # date 对象可以直接比较大小
            raise ValueError("结束日期不能早于开始日期")

        # 两个 date 相减得到 timedelta，.days 取相差的天数；首尾都算，所以 +1
        expected = (self.end_date - self.start_date).days + 1
        if self.travel_days != expected:
            raise ValueError(f"travel_days 应为 {expected}，实际为 {self.travel_days}")
        return self  # model_validator(mode="after") 必须返回 self
```

### 第二段：行程积木（坐标、景点、餐饮、酒店、天气）

```python
# ============================================================
# 响应模型：后端返回的旅行计划，自底向上搭建
# ============================================================

class Location(BaseModel):
    """经纬度坐标（第 10 课在地图上打点要用）"""

    # 用约束挡住明显错误的坐标，比如大模型把经纬度写反了、写成 999 之类
    longitude: float = Field(..., ge=-180, le=180, description="经度")
    latitude: float = Field(..., ge=-90, le=90, description="纬度")


class Attraction(BaseModel):
    """景点"""

    name: str = Field(..., description="景点名称")
    address: str = Field(default="", description="地址")
    location: Location = Field(..., description="经纬度")
    visit_duration: int = Field(default=120, description="建议游览时长（分钟）")
    description: str = Field(default="", description="景点介绍")
    ticket_price: int = Field(default=0, ge=0, description="成人门票价格（元/人）")


class Meal(BaseModel):
    """一餐"""

    # Literal 限定餐别，前端可以放心地用 type 做 switch 判断
    type: Literal["breakfast", "lunch", "dinner", "snack"] = Field(..., description="餐别")
    name: str = Field(..., description="餐厅或菜品名称")
    estimated_cost: int = Field(default=0, ge=0, description="预估费用（元/人）")


class Hotel(BaseModel):
    """酒店"""

    name: str = Field(..., description="酒店名称")
    address: str = Field(default="", description="地址")
    location: Location | None = Field(default=None, description="经纬度，可能缺失")
    estimated_cost: int = Field(default=0, ge=0, description="预估费用（元/间/晚）")


class WeatherInfo(BaseModel):
    """某一天的天气"""

    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_weather: str = Field(default="", description="白天天气")
    # int | str：通常是数字；远期天气查不到时，允许是 "未知" 这样的文字
    day_temp: int | str = Field(default=0, description="白天温度（℃）")
    night_temp: int | str = Field(default=0, description="夜间温度（℃）")

    # mode="before"：在类型校验之前执行，拿到的是原始输入值
    # 场景：外部 API 或大模型返回的温度可能是 "25°C"、"15℃"，先清洗成数字再交给类型校验
    # 一个校验器可以同时作用于多个字段
    @field_validator("day_temp", "night_temp", mode="before")
    @classmethod
    def parse_temperature(cls, v):
        if isinstance(v, str):  # isinstance 判断类型，相当于 JS 的 typeof v === "string"
            v = v.replace("°C", "").replace("℃", "").replace("°", "").strip()
            try:
                return int(v)  # "25" → 25
            except ValueError:  # 转不了数字，比如 "未知"，就保留原文
                return v or "未知"
        return v
```

### 第三段：组装成完整行程

```python
class DayPlan(BaseModel):
    """一天的行程"""

    # 输出模型的日期用 str（和参考仓库保持一致）：
    # 这部分数据以后由大模型生成，第 7 课会专门讲怎么校验大模型的输出
    date: str = Field(..., description="日期 YYYY-MM-DD")
    day_index: int = Field(..., ge=0, description="第几天，从 0 开始")
    description: str = Field(..., description="当日行程概述")
    hotel: Hotel | None = Field(default=None, description="当晚住宿；最后一天可能没有")
    # list[Attraction]：数组里的每一项都会按 Attraction 校验
    attractions: list[Attraction] = Field(default_factory=list, description="景点列表")
    meals: list[Meal] = Field(default_factory=list, description="餐饮列表")


class Budget(BaseModel):
    """预算汇总（单位：元）"""

    total_attractions: int = Field(default=0, description="门票合计")
    total_hotels: int = Field(default=0, description="住宿合计")
    total_meals: int = Field(default=0, description="餐饮合计")
    total_transportation: int = Field(default=0, description="交通合计")

    # 总价由分项算出来，永远不会出现"分项加起来不等于总价"的情况
    # 后面让大模型填预算时，这一点尤其重要：大模型很不擅长做加法
    @computed_field
    @property
    def total(self) -> int:
        return self.total_attractions + self.total_hotels + self.total_meals + self.total_transportation


class TripPlan(BaseModel):
    """完整的旅行计划"""

    city: str
    start_date: str
    end_date: str
    days: list[DayPlan] = Field(..., description="每日行程")
    weather_info: list[WeatherInfo] = Field(default_factory=list, description="天气预报")
    overall_suggestions: str = Field(default="", description="总体建议")
    budget: Budget | None = Field(default=None, description="预算汇总")


class TripPlanResponse(BaseModel):
    """统一的响应外壳：{ success, message, data }"""

    # 前端常见的约定：用统一结构包一层，前端拦截器可以统一判断 success
    success: bool
    message: str = ""
    data: TripPlan | None = None  # 失败时 data 为 null
```

## Step 2：mock 行程生成器 `app/services/mock_planner.py`

```python
"""mock 行程生成器：根据请求生成结构正确的假数据，第 6 课会换成大模型"""

from datetime import timedelta  # 时间间隔，date + timedelta(days=1) = 第二天

from app.models.schemas import (
    Attraction, Budget, DayPlan, Hotel, Location, Meal, TripPlan, TripRequest, WeatherInfo,
)


def build_mock_plan(request: TripRequest) -> TripPlan:
    """输入是校验过的 TripRequest，输出是 TripPlan 对象（不是 dict）"""

    # 类型标注 list[DayPlan]：告诉编辑器这个列表里放的是 DayPlan，补全会更准确
    days: list[DayPlan] = []

    # range(n) 生成 0, 1, ..., n-1，相当于 JS 的 for (let i = 0; i < n; i++)
    for i in range(request.travel_days):
        # request.start_date 已经是 date 对象（Pydantic 帮我们转好了），可以直接做日期运算
        current = request.start_date + timedelta(days=i)

        # 用模型类创建对象：传错字段名、传错类型都会立刻报错
        # 前端类比：const day: DayPlan = {...}，但这里在运行时也会检查
        days.append(
            DayPlan(
                date=current.isoformat(),  # date → "2026-10-01"
                day_index=i,
                description=f"第 {i + 1} 天：{request.city}城市漫步",
                hotel=Hotel(name=f"{request.city}示例酒店", estimated_cost=300),
                attractions=[
                    Attraction(
                        name=f"{request.city}示例景点{i + 1}",
                        location=Location(longitude=116.397, latitude=39.909),
                        ticket_price=60,
                    )
                ],
                meals=[
                    Meal(type="lunch", name="示例午餐", estimated_cost=50),
                    Meal(type="dinner", name="示例晚餐", estimated_cost=80),
                ],
            )
        )

    # 列表推导式：每天生成一条天气，相当于 days.map(d => new WeatherInfo(...))
    # 故意传 "25°C" 字符串，验证 WeatherInfo 的 before 校验器会把它转成 25
    weather = [
        WeatherInfo(date=d.date, day_weather="晴", day_temp="25°C", night_temp="15℃")
        for d in days
    ]

    people = request.party.total  # 计算字段，用起来和普通属性一样

    # sum(... for ... in ...) 是生成器表达式，相当于 JS 的 .flatMap(...).reduce((a, b) => a + b, 0)
    # 注意计价单位：门票、餐饮按人头算，所以要乘人数；酒店按房间算，不乘人数
    budget = Budget(
        total_attractions=sum(a.ticket_price for d in days for a in d.attractions) * people,
        total_hotels=sum(d.hotel.estimated_cost for d in days if d.hotel),
        total_meals=sum(m.estimated_cost for d in days for m in d.meals) * people,
        total_transportation=50 * request.travel_days,
    )

    return TripPlan(
        city=request.city,
        start_date=request.start_date.isoformat(),
        end_date=request.end_date.isoformat(),
        days=days,
        weather_info=weather,
        overall_suggestions="这是 mock 数据，第 6 课会换成大模型生成。",
        budget=budget,
    )
```

## Step 3：在 `app/api/routes/trip.py` 加 POST 接口

在文件顶部的 import 区域追加：

```python
from app.models.schemas import TripPlanResponse, TripRequest  # 请求、响应模型
from app.services.mock_planner import build_mock_plan          # mock 生成器
```

在文件末尾追加：

```python
# POST /api/trip/plan
# response_model：声明响应结构，有三个作用
#   1. /docs 里会展示完整的响应结构
#   2. 返回前会按模型校验并序列化（返回的数据结构不对，后端直接报错，不会把错误数据发给前端）
#   3. 过滤掉模型里没有声明的多余字段，避免不小心把内部数据泄露给前端
@router.post("/plan", response_model=TripPlanResponse, summary="生成旅行计划")
def plan_trip(request: TripRequest):
    # 参数类型是 Pydantic 模型 → FastAPI 自动从请求体（JSON body）读取并校验
    # 相当于 Express 的 req.body，但已经校验并转好类型了
    # 能执行到这里，说明 request 一定是合法的（校验失败时 FastAPI 直接返回 422，不会进入函数）
    plan = build_mock_plan(request)
    return TripPlanResponse(success=True, message="旅行计划生成成功（mock）", data=plan)
```

> **FastAPI 怎么判断参数从哪里来？**
> - 类型是 `int`、`str` 这种简单类型，并且出现在路径 `{}` 里 → 路径参数
> - 简单类型，但没出现在路径里 → 查询参数
> - 类型是 **Pydantic 模型** → **请求体（JSON body）**

## Step 4：验证

```bash
uv run python run.py
```

打开 http://127.0.0.1:7001/docs，找到 `POST /api/trip/plan` → "Try it out"。示例请求体已经自动填好了（这就是 `json_schema_extra` 的作用），直接点 Execute。

也可以用 curl：

```bash
# -X POST 指定方法，-H 设置请求头，-d 是请求体
# 注意 city 前后故意带了空格，看看返回结果里有没有被去掉
curl -s -X POST http://127.0.0.1:7001/api/trip/plan \
  -H "Content-Type: application/json" \
  -d '{"city":" 北京 ","start_date":"2026-10-01","end_date":"2026-10-02","travel_days":2,"party":{"adults":2,"children":1}}' \
  | python3 -m json.tool --no-ensure-ascii
# python3 -m json.tool：格式化 JSON 输出；--no-ensure-ascii：中文正常显示，不转成 \uXXXX
```

检查返回结果：

- `city` 是 `"北京"`，首尾空格被去掉了（`field_validator` 生效）。
- `day_temp` 是 `25`，不是 `"25°C"`（`mode="before"` 校验器生效）。
- `budget.total` = 360 + 600 + 780 + 100 = **1840**（`computed_field` 生效）。

再故意传几个错误数据，观察 422 响应：

```bash
# 日期不存在 + 成人数为 0 + strictness 不在可选值里：一次性返回 3 条错误
curl -s -X POST http://127.0.0.1:7001/api/trip/plan \
  -H "Content-Type: application/json" \
  -d '{"city":"北京","start_date":"2026-13-01","end_date":"2026-10-03","travel_days":3,"party":{"adults":0},"budget_constraint":{"strictness":"maybe"}}' \
  | python3 -m json.tool --no-ensure-ascii

# 天数和日期对不上：model_validator 报错，注意它的 loc 是 ["body"]，不指向某个具体字段
curl -s -X POST http://127.0.0.1:7001/api/trip/plan \
  -H "Content-Type: application/json" \
  -d '{"city":"北京","start_date":"2026-10-01","end_date":"2026-10-03","travel_days":5,"party":{"adults":2}}' \
  | python3 -m json.tool --no-ensure-ascii
```

> Pydantic 会**一次性收集所有字段错误**再返回，不是遇到第一个错误就停下。前端可以根据每条错误的 `loc` 路径，把错误提示显示在对应的表单项下面。
>
> 不过有个例外：`model_validator(mode="after")` 只有在所有字段都校验通过之后才会执行。所以上面第一个请求里，"日期对不上"这条错误不会出现。

## 📝 课后作业

- [ ] 1. **补一个校验**：`preferences` 最多允许 5 个标签，并且每个标签要去掉首尾空格、过滤掉空字符串。（提示：用 `field_validator`，`Field` 里也有 `max_length` 可以限制列表长度。想想哪种写法更合适。）
- [ ] 2. **观察 `response_model` 的过滤作用**：在 `plan_trip` 里改成直接返回 dict：`{"success": True, "data": plan, "secret": "内部数据"}`，请求一次，看看 `secret` 字段有没有出现在响应里。然后把 `response_model=...` 删掉再试一次。记录两次的结果和原因，最后把代码改回去。
- [ ] 3. **写对应的 TS 类型**：在 `doc/` 的笔记区写出 `TripRequest` 和 `TripPlan` 对应的 TypeScript `interface`（第 4 课前端会直接用）。思考：`computed_field` 的 `total` 在 TS 里应该怎么体现？请求和响应里的 `PartyInfo` 是否应该是同一个 interface？
- [ ] 4. **思考题**：
  - 为什么 `TripRequest` 的日期用 `date`，而 `DayPlan` 的日期用 `str`？
  - mock 预算里，为什么酒店费用不乘人数，门票和餐饮要乘？如果 3 个人住一间房不够，应该怎么改？（不用写代码，说出思路即可。参考仓库为此专门设计了"预算账本"）

**提交方式**：把 `schemas.py` 的改动（作业 1）、作业 2 的观察结果、作业 3 的 TS 类型和思考题答案发给老师。下一课：前端接入（Vue 3 + Vite + axios 调这个 mock 接口）。

## 我的笔记

<!-- 在这里记录你的答案和心得 -->
