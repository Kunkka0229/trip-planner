"""FastAPI 主应用：创建 app、挂中间件、注册路由"""
from contextlib import asynccontextmanager  # 用来写 lifespan 的工具

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware  # FastAPI 自带的 CORS 中间件

from app.api.routes import demo, trip, poi  # 导入路由模块
from app.api.routes import map as map_routes  # map 和 Python 内置函数 map() 重名，起个别名避免覆盖
from app.services.amap_client import AmapError

from app.config import get_settings

settings = get_settings()  # 取配置中心的配置对象

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
    print("=" * 50)  # 字符串乘法：把 "=" 重复 50 次
    print(f"🚀 {settings.app_name} v{settings.app_version}")
    print(f"   监听地址: {settings.host}:{settings.port}")
    print(f"   高德 Key: {status_of(settings.amap_api_key)}")
    print(f"   LLM Key : {status_of(settings.llm_api_key)}")
    print(f"   CORS 允许: {settings.cors_origins_list}")
    print("=" * 50)
    yield  # ← 服务在这里运行，直到收到关闭信号
    print("👋 服务已关闭")
    
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
)

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