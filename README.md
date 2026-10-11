# Trip Planner · 智能旅行助手

输入城市、日期、同行人员和偏好，由大模型结合高德地图的真实数据，生成一份逐天的旅行计划：景点、三餐、酒店、天气和预算。

这是一个**从零手写的学习项目**，参考 Datawhale [Hello-Agents](https://github.com/datawhalechina/hello-agents) 第 13 章"智能旅行助手"和它的[参考实现](https://github.com/nameless0120/helloagents-trip-planner)，Agent 层改用了 LangChain + LangGraph。每一步的实现过程都记录在 [`doc/`](doc/) 里的课程讲义中。

## 功能

- **行程生成**：先由代码并行查询高德（景点、酒店、餐厅、天气），整理成结构化的上下文，再交给大模型规划
- **输出可靠性**：从模型的回复里提取 JSON、用 Pydantic 校验、检查业务规则（天数、三餐、景点数），不合格就带着错误原因重试，多次失败后返回兜底方案
- **Grounding**：模型选的景点、酒店、餐厅必须来自高德的候选，坐标和价格都以真实数据为准
- **预算**：门票按价格表（区分淡旺季）、餐饮按高德人均、酒店按档次估算，总价由代码计算
- **实时进度**：SSE 推送生成进度
- **结果页**：地图打点、预算明细、行程编辑、导出
- **多种规划模式**（`PLANNER_MODE`）：代码收集上下文 + 单 Agent 规划（默认）、直连模式、书中的多 Agent + MCP 模式

## 技术栈

| | |
|---|---|
| 后端 | Python 3.13 · FastAPI · Pydantic v2 · LangChain / LangGraph · langchain-mcp-adapters · httpx · uv |
| 前端 | Vue 3 · TypeScript · Vite · Ant Design Vue · 高德 JS API |
| 外部服务 | 高德 Web 服务 API · 任意 OpenAI 兼容的大模型接口（DeepSeek、阿里云百炼等） |

## 生成一份行程的流程

```
TripRequest
  ├─ 并行查询高德：景点 / 酒店 / 餐厅 / 天气，附上价格提示，记录每个工具的成败
  ├─ 没有景点数据 → 直接返回兜底方案，不调用大模型
  ├─ 精简上下文 → 大模型生成 JSON
  ├─ 提取 JSON → Pydantic 校验 → 业务规则校验   ── 不合格：带上错误原因重试（最多 3 次）
  ├─ Grounding：匹配回真实候选，用真实坐标和价格覆盖模型的输出
  └─ 按统一口径重算预算 → 返回
```

## 目录结构

```
backend/
├── app/
│   ├── api/          # FastAPI 应用和路由（trip / poi / map）
│   ├── agents/       # 规划器、提示词、输出校验、重试与兜底
│   ├── planner/      # PlannerContext、价格提示、Grounding、门票价格表
│   ├── services/     # 高德 HTTP 客户端、高德 MCP、大模型
│   ├── models/       # Pydantic 数据模型
│   └── config.py     # 配置（读取 .env）
└── run.py            # 启动入口
frontend/
└── src/              # 表单页、结果页、地图组件、API 封装
doc/                  # 课程讲义（基础课 11 课 + 进阶课 13 课）
```

## 快速开始

需要先准备：

| Key | 用途 | 申请 |
|---|---|---|
| 高德 Web 服务 Key | 后端查询景点、天气 | [高德开放平台](https://lbs.amap.com/)，服务平台选"Web 服务" |
| 高德 JS API Key + 安全密钥 | 前端地图 | 同上，服务平台选"Web端(JS API)" |
| 大模型 API Key | 生成行程 | 任意 OpenAI 兼容接口 |

> 没有大模型 Key 时，接口会返回 mock 数据，前端照样可以开发和预览。

### 后端

需要 [uv](https://docs.astral.sh/uv/)。

```bash
cd backend
cp .env.example .env      # 填写 AMAP_API_KEY、LLM_API_KEY、LLM_BASE_URL、LLM_MODEL_ID
uv sync
uv run python run.py      # http://127.0.0.1:7001，接口文档在 /docs
```

### 前端

```bash
cd frontend
cp .env.example .env      # 填写 VITE_AMAP_JS_KEY、VITE_AMAP_SECURITY_CODE
npm install
npm run dev               # http://localhost:5173，/api 请求由 Vite 代理到后端
```

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/trip/plan` | 生成旅行计划 |
| POST | `/api/trip/plan/stream` | 生成旅行计划，SSE 推送进度 |
| GET | `/api/trip/health` | 检查高德、大模型是否已配置 |
| GET | `/api/poi/search` | 关键字搜索地点 |
| GET | `/api/map/weather` | 天气预报 |

完整的请求和响应结构见后端启动后的 `/docs`。

## 课程讲义

- **基础课**（[`doc/00-学习计划.md`](doc/00-学习计划.md)）：从 FastAPI 起步，到接入高德和大模型、输出校验、多 Agent + MCP、PlannerContext 重构、结果页和测试，共 11 课。
- **进阶课**（[`doc/进阶/00-进阶学习计划.md`](doc/进阶/00-进阶学习计划.md)）：面向生产环境的 Agent 开发，共 13 课：LangGraph 状态图、Langfuse 可观测、评测体系、流式输出、会话持久化与多轮修改、人工确认、长期记忆、自建 MCP Server、Agentic RAG、多 Agent 架构、护栏与安全、成本与可靠性、Docker 部署。

> 进阶课目前只有讲义，对应的代码还没有合进本仓库。

## 致谢与许可

- 项目思路来自 Datawhale [Hello-Agents](https://github.com/datawhalechina/hello-agents) 第 13 章，架构参考 [nameless0120/helloagents-trip-planner](https://github.com/nameless0120/helloagents-trip-planner)。
- `backend/app/planner/attraction_price_table.json` 复制自上述参考仓库，采用 **CC BY-NC-SA 4.0** 许可（署名、非商业使用、相同方式共享）。表中的票价是估算值，不是官方实时价格，只用于预算参考。
- 本项目仅用于学习。
