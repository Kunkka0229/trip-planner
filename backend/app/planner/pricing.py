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

# 只在第一次调用时读文件，之后直接返回缓存（和 get_settings 一样的单例技巧）
@lru_cache
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