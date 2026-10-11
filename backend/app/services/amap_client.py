"""高德地图 Web 服务 API 客户端（直接走 HTTP，不经过大模型）

官方文档：https://lbs.amap.com/api/webservice/summary
本课用到两个接口：
  - 关键字搜索 POI：GET /v3/place/text
  - 天气查询：    GET /v3/weather/weatherInfo
"""

import threading
import time
from functools import lru_cache
from typing import Any

import httpx  # Python 的 HTTP 客户端，API 风格和 axios 很像；装 fastapi[standard] 时已经带上了

from app.config import get_settings
from app.models.schemas import Location, POIInfo, WeatherInfo

# 高德地图 Web 服务 API 的基础 URL
AMAP_BASE_URL = "https://restapi.amap.com/v3"

class AmapError(Exception):
    """高德调用失败时抛出的业务异常（自定义异常类，相当于 JS 的 class AmapError extends Error）"""
    global _last_request_at        # 要修改模块级变量，必须先声明 global
    

def parse_location(text: Any) -> Location | None:
    """高德返回的坐标是字符串 "116.397,39.909"（经度在前），转成 Location 对象"""

    if not isinstance(text, str) or "," not in text:
        return None  # 有些 POI 没有坐标，或者返回的是 []
    # split(",", 1)：最多切一刀
    lng, lat = text.split(",", 1)
    try:
        return Location(longitude=float(lng), latitude=float(lat))
    except ValueError:
        # 转 float 失败，或者超出经纬度范围（Pydantic 校验失败也是 ValueError 的子类）
        return None
    
def parse_float(value: Any) -> float | None:
    """高德的 rating/cost 可能是 "4.6"、""、[]，统一转成 float 或 None"""
    try:
        return float(value)
    except (ValueError, TypeError): # 一次捕获多种异常，写成元组
        return None

def as_text(value: Any) -> str:
    """高德的一个坑：字段没有值时，返回的不是 "" 而是 []，统一转成字符串"""
    return value if isinstance(value, str) else ""

class AmapClient:
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
    
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:        
        """所有请求的统一入口：限速、带上 key、处理网络错误、处理高德业务错误"""

        self._wait_for_slot()  # 第 9 课：所有高德请求都经过这里，在入口处统一排队

        try:
            # {**params, "key": ...} 是字典展开，相当于 JS 的 { ...params, key: ... }
            response = self.http.get(path, params={**params, "key": self.api_key})
            # HTTP 状态码不是 2xx 时抛出异常
            response.raise_for_status()
        except httpx.HTTPStatusError as e: # 高德返回了 4xx/5xx
            # 注意：不要直接写 f"{e}"！httpx 的异常信息里带着完整 URL，URL 里有 key=你的Key，
            # 这条信息会被返回给前端、写进日志，等于把 Key 公开了
            raise AmapError(f"请求高德失败: HTTP {e.response.status_code}") from e
        except httpx.HTTPError as e: # 超时、连不上……
            # type(e).__name__ 只取异常类名，例如 ReadTimeout、ConnectError
            # raise ... from e：保留原始异常链，排查问题时在服务端日志里能看到根本原因
            raise AmapError(f"请求高德失败: {type(e).__name__}") from e
        
        data = response.json()
        # 高德的另一个坑：业务失败时 HTTP 状态码照样是 200，要看 body 里的 status
        # status="1" 成功，"0" 失败，info 是错误原因（例如 INVALID_USER_KEY）
        if data.get("status") != "1":
            raise AmapError(f"高德返回错误: {data.get('info')}（infocode={data.get('infocode')}）")
        
        return data
    
    def search_poi(self, keyword: str, city: str, limit: int = 10) -> list[POIInfo]:
        """关键字搜索 POI，例如 keywords="博物馆", city="北京" """
        data = self._get(
            "/place/text",
            {
                "keywords": keyword,
                "city": city,
                "citylimit": "true",  # 只在指定城市内搜索
                "extensions": "all",  # 返回详细信息（包括评分、人均消费）
                "offset": limit,  # 每页条数
                "page": 1,
            }
        )
        
        results: list[POIInfo] = []
        for poi in data.get("pois", []):
            location = parse_location(poi.get("location"))
            if location is None:
                continue  # 没坐标的 POI 没法在地图上显示，直接丢掉
            biz_ext = poi.get("biz_ext") or {}  # `or {}`：值为 None 或 [] 时用空字典兜底

            results.append(
                POIInfo(
                    id=poi.get("id", ""),
                    name=poi.get("name", ""),
                    type=as_text(poi.get("type")),
                    address=as_text(poi.get("address")),
                    district=as_text(poi.get("adname")),
                    location=location,
                    tel=as_text(poi.get("tel")),
                    rating=parse_float(biz_ext.get("rating")),
                    cost=parse_float(biz_ext.get("cost")),
                )
            )
        return results
    
    def get_weather(self, city: str) -> list[WeatherInfo]:
        """查询未来几天（通常是今天起 4 天）的天气预报"""
        data = self._get(
            "/weather/weatherInfo",
            {
                "city": city,
                "extensions": "all",  # all = 预报，base = 实况
            }
        )
        forecasts = data.get("forecasts") or []
        if not forecasts:
            return []
        
        return [
            WeatherInfo(
                date=cast.get("date", ""),
                day_weather=cast.get("dayweather", ""),
                day_temp=cast.get("daytemp", "未知"),  # 是字符串 "25"，交给第 3 课写的 before 校验器转成 int
                night_temp=cast.get("nighttemp", "未知"),
            )
            for cast in forecasts[0].get("casts", [])  # forecasts[0] 是指定城市的天气预报
        ]
        
    
# 整个进程共用一个客户端（连接池可以复用），和 get_settings 一样用 lru_cache 实现单例
def get_amap_client() -> AmapClient:
    settings = get_settings()
    return AmapClient(settings.amap_api_key, qps_limit=settings.amap_qps_limit)