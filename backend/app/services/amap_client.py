"""高德地图 Web 服务 API 客户端（直接走 HTTP，不经过大模型）

官方文档：https://lbs.amap.com/api/webservice/summary
本课用到两个接口：
  - 关键字搜索 POI：GET /v3/place/text
  - 天气查询：    GET /v3/weather/weatherInfo

P4：同时提供同步（search_poi）和异步（asearch_poi）两套方法，和 LangChain 的 invoke / ainvoke 一个思路
  - 请求参数的拼装、响应的解析是纯函数，两套方法共用（"把 IO 和逻辑分开"）
  - 只有"发请求"这一步分成同步和异步两种写法
"""

import asyncio
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
    """高德调用失败时抛出的业务异常（自定义异常类，相当于 JS 的 class AmapError extends Error）"""      # 要修改模块级变量，必须先声明 global
    

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

# ---------- 纯函数：参数拼装和响应解析（同步、异步共用）----------
def poi_params(keyword: str, city: str, limit: int) -> dict[str, Any]:
    return {
        "keywords": keyword,
        "city": city,
        "citylimit": "true",  # 只在指定城市内搜索
        "extensions": "all",  # 返回详细信息（包括评分、人均消费）
        "offset": limit,  # 每页条数
        "page": 1,
    }

def parse_pois(data: dict[str, Any]) -> list[POIInfo]:
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

def weather_params(city: str) -> dict[str, Any]:
    return {"city": city, "extensions": "all"}  # all = 预报，base = 实况

def parse_weather(data: dict[str, Any]) -> list[WeatherInfo]:
    forecasts = data.get("forecasts") or []
    if not forecasts:
        return []
    return [
        WeatherInfo(
            date=cast.get("date", ""),
            day_weather=cast.get("dayweather", ""),
            day_temp=cast.get("daytemp", "未知"),
            night_temp=cast.get("nighttemp", "未知"),
        )
        for cast in forecasts[0].get("casts", [])
    ]
    
def check_response(response: httpx.Response) -> dict[str, Any]:
    """高德的另一个坑：业务失败时 HTTP 状态码照样是 200，要看 body 里的 status"""
    data = response.json()
    if data.get("status") != "1":
        raise AmapError(f"高德返回错误: {data.get('info')}（infocode={data.get('infocode')}）")
    return data

def wrap_http_error(e: httpx.HTTPError) -> AmapError:
    """注意：不要直接写 f"{e}"！httpx 的异常信息里带着完整 URL，URL 里有 key=你的Key"""
    if isinstance(e, httpx.HTTPStatusError):
        return AmapError(f"请求高德失败: HTTP {e.response.status_code}")
    return AmapError(f"请求高德失败: {type(e).__name__}")

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

        # P4：异步客户端，相当于 axios 的异步版本。await self.ahttp.get(...) 等待期间，事件循环可以去处理别的请求
        self.ahttp = httpx.AsyncClient(base_url=AMAP_BASE_URL, timeout=10, transport=transport)

        # ---------- 限速（第 9 课，P4 改成"预约时间槽"的写法）----------
        # 两次请求之间至少间隔多少秒：每秒 3 次 → 间隔 1/3 秒
        self.min_interval = 1 / qps_limit if qps_limit > 0 else 0
        # 线程锁：第 9 课用线程池并行调高德，锁保证同一时刻只有一个线程在"检查间隔、更新时间"
        self._lock = threading.Lock()
        self._next_slot = 0.0  # 下一个可用的发送时间点
        
    def _reserve_wait(self) -> float:
        """预约一个发送时间槽，返回需要等待的秒数

            第 9 课的写法是"拿着锁睡觉"：time.sleep 在锁里面，异步代码不能这样做（会卡住整个事件循环）
            新写法：锁里只做一次很快的计算（预约下一个空闲时间点），睡觉放在锁外面。
            同步方法用 time.sleep 等，异步方法用 asyncio.sleep 等，两边共用同一个时间表
        """
        if self.min_interval <= 0:
            return 0.0
        with self._lock:  # 拿到锁才能往下执行，其他线程在这里排队等待
            # time.monotonic()：只增不减的时钟，专门用来算时间间隔（相当于前端的 performance.now()）
            now = time.monotonic()
            slot = max(now, self._next_slot)  # 最早能发的时间：现在，或者排在前面的人之后
            self._next_slot = slot + self.min_interval  # 下一个人至少要再等一个间隔

            return slot - now
    
    def _with_key(self, params: dict[str, Any]) -> dict[str, Any]:
        return {**params, "key": self.api_key}
    
    # ---------- 同步：给 poi / map 路由和旧的规划器用 ----------
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        time.sleep(self._reserve_wait())
        try:
            response = self.http.get(path, params=self._with_key(params))
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise wrap_http_error(e) from e
        return check_response(response)
    
    def search_poi(self, keyword: str, city: str, limit: int = 10) -> list[POIInfo]:
        return parse_pois(self._get("/place/text", poi_params(keyword, city, limit)))
    
    def get_weather(self, city: str) -> list[WeatherInfo]:
        return parse_weather(self._get("/weather/weatherInfo", weather_params(city)))
    
    # ---------- 异步（P4）：给 LangGraph 的异步节点用 ----------
    async def _aget(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(self._reserve_wait())  # asyncio.sleep 不会卡住事件循环，time.sleep 会
        try:
            response = await self.ahttp.get(path, params=self._with_key(params))
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise wrap_http_error(e) from e
        return check_response(response)
    
    async def asearch_poi(self, keyword: str, city: str, limit: int = 10) -> list[POIInfo]:
        return parse_pois(await self._aget("/place/text", poi_params(keyword, city, limit)))

    async def aget_weather(self, city: str) -> list[WeatherInfo]:
        return parse_weather(await self._aget("/weather/weatherInfo", weather_params(city)))
        
    
# 整个进程共用一个客户端（连接池和限速的时间表都要共享），和 get_settings 一样用 lru_cache 实现单例
# P4 修复：之前漏了 @lru_cache，每个请求都会新建一个客户端，各自限速，等于没限速
@lru_cache
def get_amap_client() -> AmapClient:
    settings = get_settings()
    return AmapClient(settings.amap_api_key, qps_limit=settings.amap_qps_limit)
