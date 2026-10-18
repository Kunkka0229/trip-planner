"""FastAPI 主应用：创建 app、挂中间件、注册路由"""
import logging
from contextlib import asynccontextmanager
import uuid  # 用来写 lifespan 的工具

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware  # FastAPI 自带的 CORS 中间件

from app.api.routes import demo, trip, poi  # 导入路由模块
from app.api.routes import map as map_routes  # map 和 Python 内置函数 map() 重名，起个别名避免覆盖
from app.services.amap_client import AmapError

from app.config import get_settings
from app.observability.logs import setup_logging, request_id_var
from app.observability.tracing import shutdown_tracing
from app.graph.checkpoint import open_checkpointer
from app.graph.trip_graph import build_trip_graph

logger = logging.getLogger(__name__)

settings = get_settings()  # 取配置中心的配置对象
setup_logging(settings.log_level)  # P2：在创建 app 之前配置好日志

def status_of(value: str) -> str:
    """密钥只显示"已配置/未配置"，避免在日志里泄露"""
    return "已配置" if value else "未配置"

# ---------- lifespan：应用的生命周期 ----------
# yield 之前的代码：服务启动时执行一次（适合：校验配置、建数据库连接、预热模型）
# yield 之后的代码：服务关闭时执行一次（适合：关闭连接、释放资源）
# 类似 Vue 组件的 onMounted / onUnmounted，只不过作用对象是整个后端服务
# 参考仓库用的 @app.on_event("startup") 是旧写法，FastAPI 已经不推荐使用
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动横幅也换成 logger：和其他日志格式统一、带时间戳；部署后进入日志平台，能查到"服务什么时候重启过、当时用的什么配置"
    # 去掉了 "=" 分隔线：日志平台按行收集，装饰性的分隔线只会变成几条没有内容的日志
    logger.info("%s v%s 启动，监听地址 %s:%s", settings.app_name, settings.app_version, settings.host, settings.port)
    logger.info("高德 Key: %s，LLM Key: %s", status_of(settings.amap_api_key), status_of(settings.llm_api_key))
    logger.info("CORS 允许: %s", settings.cors_origins_list)

    # P5：打开 checkpointer，编译带持久化的图，挂到 app.state 上（app.state 是 FastAPI 提供的"全局存储"）
    # 用 async with 包住 yield：服务运行期间连接一直打开，服务关闭时自动关闭
    async with open_checkpointer(settings.checkpoint_url) as checkpointer:
        app.state.trip_graph = build_trip_graph(checkpointer=checkpointer)
        yield  # ← 服务在这里运行，直到收到关闭信号
    shutdown_tracing()  # P2：把还没上报的 trace 发完再退出
    logger.info("服务已关闭")
    
# 创建应用：标题和版本从配置读取，生命周期交给 lifespan
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
    docs_url="/docs",  # Swagger UI 文档
    redoc_url="/redoc",  # ReDoc 文档
)

# ---------- CORS 中间件 ----------
# 浏览器的同源策略：前端在 localhost:5173，后端在 localhost:7000，端口不同就算"跨域"，
# 浏览器会拦下响应。后端在响应头里加上 Access-Control-Allow-Origin，浏览器才会放行。
# 相当于 Express 里的 app.use(cors({ origin: [...] }))
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,  # 只允许这些前端地址
    allow_credentials=True,                    # 允许携带 cookie
    allow_methods=["*"],                       # 允许所有 HTTP 方法（GET/POST/...）
    allow_headers=["*"],                       # 允许所有请求头
    expose_headers=["X-Request-ID"],           # P2：跨域时前端 JS 默认读不到自定义响应头，要显式暴露
)

# ---------- request_id 中间件（P2）----------
# 每个请求分配一个 ID：优先用调用方传来的 X-Request-ID（网关、前端都可能传），没有就生成一个
# 这个 ID 会出现在：本次请求的每一行日志、响应头、Langfuse 的 trace_id（由它算出来）
# 用户报错时只要把响应头里的 X-Request-ID 发给你，就能找到这次请求的全部信息
@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    token = request_id_var.set(request_id)  # set 返回一个 token，用来在结束时恢复原值
    try:
        response = await call_next(request)  # 交给后面的中间件和路由处理，相当于 Express 的 next()
    finally:
        request_id_var.reset(token)
    response.headers["X-Request-ID"] = request_id
    return response

# ---------- 全局异常处理器 ----------
# 任何路由里抛出 AmapError，都会被这里接住，转成 502 + {"detail": "..."}
# 和 Express 的错误处理中间件 app.use((err, req, res, next) => ...) 是一个思路：
# 路由只管业务，错误格式在一处统一
@app.exception_handler(AmapError)
async def handle_amap_error(request: Request, exc: AmapError):
    return JSONResponse(status_code=502, content={"detail": str(exc)})


# ---------- 注册路由 ----------
# 相当于 Express 的 app.use('/api', tripRouter)
# prefix="/api" 加上路由文件里自己的 prefix，组成完整路径
app.include_router(demo.router, prefix="/api")  # demo 测试接口
app.include_router(trip.router, prefix="/api")  # 旅行规划接口
app.include_router(poi.router, prefix="/api")
app.include_router(map_routes.router, prefix="/api")

@app.get("/")
def root():
    """根路径：返回服务基本信息"""
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
    }


@app.get("/health")
def health():
    """服务级健康检查：只要进程活着就返回 healthy"""
    return {"status": "healthy"}