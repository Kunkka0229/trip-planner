"""启动脚本：python run.py"""

import uvicorn  # ASGI 服务器，真正负责监听端口

from app.config import get_settings

if __name__ == "__main__":
    settings = get_settings()

    uvicorn.run(
        # 用字符串 "模块路径:变量名" 指定 app，而不是直接传 app 对象
        # 原因：reload=True 时，uvicorn 要在子进程里重新 import 你的代码，只能用字符串
        # app.api.main 对应文件 app/api/main.py，app 是里面的变量名
        "app.api.main:app",
        host=settings.host,
        port=settings.port,                       # 端口从配置读取
        reload=True,                              # 代码改动后自动重启（仅开发环境）
        log_level=settings.log_level.lower(),     # uvicorn 要求小写：info / debug
    )