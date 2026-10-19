"""模拟的酒店预订服务（P6）

真实项目里这里会调用携程、飞猪等平台的接口，涉及真金白银。所以要演示两件事：
  1. 调用之前必须有用户明确的确认（HumanInTheLoopMiddleware）
  2. 幂等：同一个请求重复提交（用户双击、网络重试、Agent 重放），只下一单
"""

import uuid
from dataclasses import dataclass
from functools import lru_cache

@dataclass
class Order:
    order_id: str
    hotel: str
    check_in: str
    nights: int
    rooms: int
    total_price: int
    
class BookingService:
    def __init__(self) -> None:
        # 幂等键 → 订单。真实系统里这是数据库表上的一个唯一索引
        self.orders: dict[str, Order] = {}
    
    async def book_hotel(
        self, 
        *, 
        hotel: str, 
        check_in: str, 
        nights: int, 
        rooms: int, 
        unit_price: int, 
        idempotency_key: str
    ) -> Order:
        # 幂等：这个键已经下过单了，直接返回原来的订单，不会重复扣款
        if idempotency_key in self.orders:
            return self.orders[idempotency_key]
        order = Order(
            order_id=f"HT{uuid.uuid4().hex[:10].upper()}",
            hotel=hotel,
            check_in=check_in,
            nights=nights,
            rooms=rooms,
            unit_price=unit_price * nights * rooms
        )
        self.orders[idempotency_key] = order
        return order
    
@lru_cache
def get_booking_service() -> BookingService:
    return BookingService()