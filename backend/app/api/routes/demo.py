"""练习用接口：第 1 课写的东西都放这里"""

from fastapi import APIRouter

# APIRouter 相当于 express.Router()：一个"迷你 app"，最后挂到主 app 上
# prefix：这个文件里所有路由的公共前缀
# tags：在 /docs 页面里把这些接口归为一组显示
router = APIRouter(prefix="/demo", tags=["练习"])


# 注意这里用的是 @router.get，不是 @app.get
# 最终地址 = include_router 的 prefix(/api) + 这里的 prefix(/demo) + 路径(/hello)
#          = /api/demo/hello
@router.get("/hello")
def hello(name: str = "旅行者"):  # 第 1 课作业 1 的参考答案
    # f"..." 是 f-string，相当于 JS 的模板字符串 `你好，${name}`
    return {"greeting": f"你好，{name}"}


# 最终地址：/api/demo/cities/{city}
@router.get("/cities/{city}")
def get_city(city: str, days: int = 3):
    return {"city": city, "days": days}