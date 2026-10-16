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
    adults: int = Field(..., ge=1, le=10, description="成人数量（1~10）")
    children: int = Field(0, ge=0, le=10, description="儿童数量（0~10）")
    infants: int = Field(0, ge=0, le=10, description="婴儿数量（0~10）")

    # 计算字段：总人数根据前三项算出来，不需要前端传
    # （参考仓库要求前端传 total，再校验它是否等于三项之和；我们直接算出来，前端少传一个字段，也就不可能传错）
    # @computed_field + @property：输出 JSON 时会带上 total，在 /docs 的响应结构里也能看到
    @computed_field
    @property
    def total(self) -> int:
        """总人数"""
        return self.adults + self.children + self.infants
    
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
        """去掉城市名首尾的空格"""
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
        # isinstance 判断类型，相当于 JS 的 typeof v === "string"
        if isinstance(v, str):
            # 去掉温度里的 °C、℃、° 等符号，剩下的就是数字字符串了
            v = v.replace("°C", "").replace("℃", "").replace("°", "").strip()
            try:
                return int(v)  # 转成整数
            except ValueError: # 转不了数字，比如 "未知"，就保留原文
                return v or "未知"
        return v
    
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
        """总预算"""
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
    trace_id: str | None = None  # P2：这次生成对应的 Langfuse trace，前端提交反馈时带回来

# ============================================================
# 高德相关模型（第 5 课新增）
# ============================================================
class POIInfo(BaseModel):
    """一个地点（Point of Interest）：景点、酒店、餐厅都是 POI"""
    
    id: str = Field(..., description="高德 POI ID")
    name: str = Field(..., description="名称")
    type: str = Field(default="", description="高德分类，例如 风景名胜;公园广场;公园")
    address: str = Field(default="", description="地址")
    district: str = Field(default="", description="所在区县")
    location: Location = Field(..., description="经纬度")
    tel: str = Field(default="", description="电话")
    rating: float | None = Field(default=None, description="评分，可能没有")
    cost: float | None = Field(default=None, description="人均消费（元），可能没有")

# 泛型响应外壳：ApiResponse[list[POIInfo]]、ApiResponse[list[WeatherInfo]]……
# class ApiResponse[T] 是 Python 3.12+ 的泛型语法，相当于 TS 的 interface ApiResponse<T>
class ApiResponse[T](BaseModel):
    success: bool = True
    message: str = ""
    data: T | None = None
    
# ============================================================
# 用户反馈（P2 新增）
# ============================================================
class FeedbackRequest(BaseModel):
    trace_id: str = Field(..., min_length=32, max_length=32, description="生成行程时返回的 trace_id")
    helpful: bool = Field(..., description="这份行程有没有帮助（👍 / 👎）")
    comment: str = Field(default="", max_length=500, description="可选的文字反馈")