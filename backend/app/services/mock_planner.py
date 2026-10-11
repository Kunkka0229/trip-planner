
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