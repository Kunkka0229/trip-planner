"""POI 搜索接口（替换第 2 课作业里的假数据版本）"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.models.schemas import ApiResponse, POIInfo
from app.services.amap_client import AmapClient, get_amap_client

router = APIRouter(prefix="/poi", tags=["POI"])

# 注意这里用的是 def，不是 async def：
# httpx.Client 是同步阻塞的，写在 def 路由里，FastAPI 会自动把它放到线程池执行，不会卡住其他请求；
# 如果写成 async def 再调用阻塞代码，整个服务在等高德返回期间都处理不了别的请求
@router.get("/search", response_model=ApiResponse[list[POIInfo]], summary="关键字搜索 POI")
def search_poi(
    # Annotated[类型, 额外信息]：Query(...) 给查询参数加校验和文档
    keywords: Annotated[str, Query(min_length=1, max_length=30, description="关键词，例如 博物馆")],
    city: Annotated[str, Query(min_length=1, description="城市")] = "北京",
    limit: Annotated[int, Query(ge=1, le=25)] = 10,
    # 依赖注入：FastAPI 调用 get_amap_client() 并把结果传进来
    # 好处：路由不关心客户端怎么创建；测试时可以用 app.dependency_overrides 换成假客户端
    amap: AmapClient = Depends(get_amap_client),
):
    # 这里不用 try/except：AmapError 由 main.py 里注册的全局异常处理器统一转成 502 响应
    pois = amap.search_poi(keywords, city, limit)
    return ApiResponse(data=pois, message=f"找到 {len(pois)} 个结果")