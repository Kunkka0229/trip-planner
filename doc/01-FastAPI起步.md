# 第 1 课：FastAPI 起步

**目标**：搞懂 Python 项目怎么组织，跑通一个带自动文档的接口，并体会 FastAPI 最核心的思想：**类型注解就是校验规则**。

## Step 0：修正项目骨架

你在 `backend/` 下执行过 `uv init`，生成的是**库（package）布局**：有 `src/backend/`、`[project.scripts]`、`[build-system]`。这种布局适合发布到 PyPI 的库，我们写的是 **Web 应用**，改成和参考仓库一致的 `app/` 布局：

1. 删除 `backend/src/` 目录。
2. 编辑 `pyproject.toml`，只保留 `[project]` 一段：

```toml
# pyproject.toml —— 相当于前端的 package.json
[project]
name = "backend"                      # 项目名
version = "0.1.0"                     # 版本号
description = "智能旅行助手后端"
readme = "README.md"
requires-python = ">=3.13"            # 要求的 Python 版本，相当于 package.json 的 engines.node
dependencies = []                     # 依赖列表，uv add 时会自动往这里写，不用手动改

# 注意：不要保留 [build-system] 和 [project.scripts]
# 只要 [build-system] 还在，uv 就会把项目当成"要构建安装的库"，
# 去找 src/backend/__init__.py，uv add 时就会报 Failed to build backend
```

3. Python 版本保持 3.13 即可：本课程用到的全部依赖（FastAPI、LangChain、LangGraph、`langchain-mcp-adapters` 等）已经在 3.13 上实测安装并运行通过。

> 以后新建应用项目，直接用 `uv init --app`（默认就是 app 布局），不要加 `--package` / `--lib`。

### uv 和前端工具对照

| Python (uv) | 前端 |
|---|---|
| `pyproject.toml` | `package.json` |
| `uv.lock` | `package-lock.json` |
| `.venv/` | `node_modules/`（连 Python 解释器本身也装在里面） |
| `.python-version` | `.nvmrc` |
| `uv add xxx` | `npm install xxx` |
| `uv run xxx` | `npx xxx`（自动使用项目的 .venv 环境） |

## Step 1：安装 FastAPI

```bash
# 进入后端目录
cd /Users/star/Desktop/star/agent/my-helloagents-trip-planner/backend

# 安装 fastapi 及其推荐依赖（[standard] 是 extras，表示"带上一组可选依赖"）
# 会自动：创建 .venv、写入 pyproject.toml 的 dependencies、生成 uv.lock
uv add "fastapi[standard]"
```

`[standard]` 里最重要的是 **uvicorn**。分工：FastAPI 负责**定义接口**，uvicorn（ASGI 服务器）负责**监听端口、把服务跑起来**。

### 坑：VSCode 提示"无法解析导入 fastapi"

VSCode 打开的根目录是 `my-helloagents-trip-planner/`，而虚拟环境在 `backend/.venv/`，Pylance 默认只认根目录下的 `.venv`，所以用了系统 Python，那里没装 fastapi。**代码其实能运行，只是编辑器找错了环境**（类似 TS 插件找不到正确的 `node_modules`）。

在 `my-helloagents-trip-planner/` 下新建 `.vscode/settings.json`：

```jsonc
{
  // 告诉 VSCode 的 Python 插件：用 backend 下的虚拟环境
  "python.defaultInterpreterPath": "${workspaceFolder}/backend/.venv/bin/python"
}
```

保存后执行 `Cmd+Shift+P` → `Developer: Reload Window`。也可以用 `Cmd+Shift+P` → `Python: Select Interpreter` 手动选。

## Step 2：建目录

```
backend/
├── app/
│   ├── __init__.py   ← 空文件，声明 app 是一个"包"，才能写 from app.xxx import ...
│   └── main.py       ← 应用入口
├── pyproject.toml
└── .python-version
```

## Step 3：第一个接口，`app/main.py`

```python
# 从 fastapi 包里导入 FastAPI 类（类似 import express from 'express'）
from fastapi import FastAPI

# 创建应用实例（类似 const app = express()）
# title 和 version 会显示在自动生成的 /docs 文档页顶部
app = FastAPI(title="Trip Planner API", version="0.1.0")


# @app.get("/") 是"装饰器"：把下面的函数注册为 GET / 的处理函数
# 等价于 Express 的 app.get('/', handler)
@app.get("/")
def root():
    # 直接返回 dict，FastAPI 会自动序列化成 JSON 响应
    # 不需要像 Express 那样写 res.json(...)
    return {"message": "Hello, Trip Planner"}


# 健康检查接口：部署后，负载均衡 / 监控会定时请求它，判断服务是否活着
@app.get("/health")
def health():
    return {"status": "healthy"}
```

## Step 4：启动（相当于 `npm run dev`）

```bash
# uv run：在项目的 .venv 环境里执行命令，不用手动 activate
# fastapi dev：开发模式启动，修改代码后自动重启（类似 nodemon / vite 热更新）
# app/main.py：入口文件，fastapi 会自动在里面找名为 app 的实例
uv run fastapi dev app/main.py
```

依次访问：

- http://127.0.0.1:8000/
- http://127.0.0.1:8000/health
- http://127.0.0.1:8000/docs ← **重点**：你没写一行文档，Swagger 就自动生成了

## Step 5：路径参数和查询参数（本课核心）

在 `app/main.py` 末尾追加：

```python
# {city} 是路径参数的占位符，类似 Express 的 '/cities/:city'
@app.get("/cities/{city}")
def get_city(
    city: str,       # 名字和路径里的 {city} 对应 → 路径参数（相当于 req.params.city）
    days: int = 3,   # 路径里没有它 → 查询参数（相当于 req.query.days）
                     # ": int" 声明类型：FastAPI 会把字符串 "5" 自动转成整数 5，
                     #         转不了（比如 "abc"）就直接返回 422 错误，函数根本不会执行
                     # "= 3"   默认值：不传 days 时就用 3
):
    # 能走到这里，说明参数已经校验通过，days 一定是 int
    return {"city": city, "days": days}
```

对比下面两个请求的返回：

- `/cities/北京?days=5`
- `/cities/北京?days=abc`

在 Express 里，你得自己写 `parseInt` 和 `isNaN` 判断；在 FastAPI 里，**类型注解本身就是校验规则**。整个项目后面都建立在这个思想上。

## 📝 课后作业

- [ ] 1. 写一个 `GET /api/hello`，接收查询参数 `name`，默认值 `"旅行者"`，返回 `{"greeting": "你好，xxx"}`。代码要带注释。
- [ ] 2. 访问 `/cities/北京?days=abc`，把返回的 422 错误体记下来，说说 `loc`、`msg`、`type` 各是什么意思。
- [ ] 3. 思考题：把 `days: int = 3` 改成 `days: int`（不给默认值），请求时又不传 `days`，会发生什么？先猜答案，再动手验证。
- [ ] 4. 在 `/docs` 页面用 "Try it out" 调一次你写的 `/api/hello`。

**提交方式**：把 `app/main.py` 的代码，以及第 2、3 题的答案发给老师。检查通过后进入第 2 课。

## 我的笔记

<!-- 在这里记录你的答案和心得 -->
