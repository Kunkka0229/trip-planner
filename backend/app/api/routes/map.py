"""地图相关接口：天气"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.models.schemas import ApiResponse, WeatherInfo
from app.services.amap_client import AmapClient, get_amap_client

router = APIRouter(prefix="/map", tags=["地图"])

@router.get("/weather", response_model=ApiResponse[list[WeatherInfo]], summary="天气预报")
def get_weather(
    city: Annotated[str, Query(min_length=1, description="城市名或 adcode")],
    amap: AmapClient = Depends(get_amap_client),
):
    return ApiResponse(data=amap.get_weather(city))