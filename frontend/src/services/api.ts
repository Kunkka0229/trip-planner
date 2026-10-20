import axios, { AxiosError } from 'axios'
import type {
  ChatRequest,
  FeedbackRequest,
  MemoryItem,
  PendingQuestion,
  ResumeRequest,
  TripPlan,
  TripPlanResponse,
  TripRequest,
  TripSession,
} from '@/types'

// 读取 .env 里的 VITE_API_BASE_URL（只有 VITE_ 开头的变量才会暴露给前端代码）
// 留空 → baseURL 为 ''，请求发到当前域名的 /api/...，由 Vite 代理转发
const baseURL = (import.meta.env.VITE_API_BASE_URL ?? '').trim().replace(/\/$/, '')

// ============================================================
// P2：匿名用户 ID
// 还没有登录系统，第一次打开页面时生成一个随机 ID 存进 localStorage，之后每次请求都放在请求头 X-User-Id 里
// 后端用它给 trace 打上 user_id，在 Langfuse 里就能按用户查找请求
// ============================================================
const USER_ID_KEY = 'tripUserId'

function createId(): string {
  // crypto.randomUUID 只在"安全上下文"（https 或 localhost）里可用
  // 用局域网 IP（http://192.168.x.x:5173）打开页面时它是 undefined，退回用随机数拼一个
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

function getUserId(): string {
  try {
    let id = localStorage.getItem(USER_ID_KEY)
    if (!id) {
      id = createId()
      localStorage.setItem(USER_ID_KEY, id)
    }
    return id
  } catch {
    // 无痕模式、禁用了存储时 localStorage 会抛异常：这次就用一个临时 ID，不影响使用
    return createId()
  }
}

// 模块加载时取一次，整个页面生命周期内不变
const userId = getUserId()

// P2：错误提示后面附上 request_id，用户截图或复制给你，你就能在日志和 Langfuse 里找到这次请求
function withRequestId(text: string, requestId: string | null | undefined): string {
  return requestId ? `${text}（如需帮助，请提供编号 ${requestId}）` : text
}

const http = axios.create({
  baseURL,
  // 大模型生成行程可能要 1~3 分钟，axios 默认不超时，这里给 5 分钟上限
  timeout: 5 * 60 * 1000,
  headers: { 'Content-Type': 'application/json', 'X-User-Id': userId },
})

// FastAPI 422 校验错误的单条结构：{ loc: ['body', 'party', 'adults'], msg: '...' }
interface ValidationErrorItem {
  loc: (string | number)[]
  msg: string
}

// 把各种错误统一转成一句人能看懂的中文
function toErrorMessage(error: unknown): string {
  if (!axios.isAxiosError(error)) return String(error)
  const err = error as AxiosError<{ detail?: string | ValidationErrorItem[] }>

  if (err.code === 'ECONNABORTED') return '请求超时，请稍后重试'
  // 没有 response = 请求根本没到后端（后端没启动、代理地址写错……）
  if (!err.response) return '无法连接后端服务，请确认后端已启动'

  // P2：后端每个响应都带 X-Request-ID 响应头（axios 会把响应头的名字转成小写）
  const requestId = err.response.headers['x-request-id'] as string | undefined
  const detail = err.response.data?.detail
  // 422：detail 是数组；去掉 loc 第一项 'body'，拼成 "party.adults: Input should be ..."
  if (Array.isArray(detail)) {
    return withRequestId(detail.map((d) => `${d.loc.slice(1).join('.') || '请求'}: ${d.msg}`).join('；'), requestId)
  }
  // HTTPException：detail 是字符串
  return withRequestId(detail || `请求失败（HTTP ${err.response.status}）`, requestId)
}

// 响应拦截器：失败时统一转换错误信息，业务代码只需要 catch 后显示 error.message
http.interceptors.response.use(
  (response) => response,
  // P5：把 HTTP 状态码也带上，调用方可以区分"没有可撤销的修改"（400）和其他错误
  (error) => Promise.reject(Object.assign(new Error(toErrorMessage(error)), { status: error.response?.status })),
)

/** 生成结果：行程、后端的提示信息，以及这次生成对应的 trace_id（P2，没启用 Langfuse 时为 null） */
export interface TripPlanResult {
  plan: TripPlan
  message: string
  traceId: string | null
  threadId: string | null // P5：会话 ID；没配置大模型（mock 数据）时为 null
  pending: PendingQuestion[] // P6：等待用户确认的问题（比如超预算），结果页会弹出确认框
}

/** 生成旅行计划：成功返回行程和后端的提示信息，失败抛出 Error */
export async function generateTripPlan(request: TripRequest): Promise<TripPlanResult> {
  const { data } = await http.post<TripPlanResponse>('/api/trip/plan', request)
  // HTTP 200 但业务失败（success=false）也当成错误抛出
  if (!data.success || !data.data) {
    throw new Error(data.message || '生成失败')
  }
  // 第 10 课：把 message 一起返回。兜底方案、grounding 警告都写在 message 里（第 7、9 课）
  return { plan: data.data, message: data.message, traceId: data.trace_id, threadId: data.thread_id, pending: data.pending }
}

// ============================================================
// 第 11 课：SSE 流式接口，接收后端推送的真实进度
// ============================================================

/**
 * P4：大模型边生成边推送的"草稿日"，还没经过后端校验，字段可能缺失，所以全部是可选的
 * 只用来在加载页预览，最终展示以 result 事件里的正式数据为准
 */
export interface DraftDay {
  date?: string
  description?: string
  hotel?: { name?: string } | null
  attractions?: { name?: string; visit_duration?: number }[]
  meals?: { type?: string; name?: string }[]
}

/** P4：collect 子图里每个高德工具跑完时推送一条（name: attractions / hotels / foods / weather） */
export interface ToolEvent {
  name: string
  ok: boolean
  count?: number
}

type StreamEvent =
  | { type: 'thread'; thread_id: string } // P5：第一个事件，告诉前端这次会话的 ID
  | { type: 'progress'; stage: string; percent: number }
  | ({ type: 'tool' } & ToolEvent)
  | { type: 'day'; index: number; day: DraftDay }
  | { type: 'reset' }
  | { type: 'result'; data: TripPlanResponse }
  | { type: 'error'; message: string }

/** P4：事件不止"进度"一种了，按类型分别回调；不关心的事件可以不传 */
export interface StreamHandlers {
  onThread?: (threadId: string) => void
  onProgress?: (stage: string, percent: number) => void
  onTool?: (tool: ToolEvent) => void
  onDay?: (index: number, day: DraftDay) => void
  onReset?: () => void
}

/**
 * 发起一个 SSE 请求，返回 request_id 和一个"事件迭代器"（P5：生成行程和对话修改共用）
 *
 * 为什么不用浏览器自带的 EventSource？它只支持 GET 请求，而我们要 POST 一个请求体
 * 所以用 fetch 拿到响应流，自己按 SSE 格式解析：每条消息是 "data: {...}\n\n"
 *
 * P4：signal 来自 AbortController。调用 controller.abort() 会中断 fetch，
 * 后端发现连接断开后会取消任务，大模型停止生成，不再继续花钱
 */
async function openSse<E>(path: string, body: unknown, signal?: AbortSignal) {
  const response = await fetch(`${baseURL}${path}`, {
    method: 'POST',
    // fetch 不经过 axios，请求头要自己带上
    headers: { 'Content-Type': 'application/json', 'X-User-Id': userId },
    body: JSON.stringify(body),
    signal,
  })
  // 响应头在开始推送事件之前就到了，先取出 request_id，后面任何一步出错都能带上它
  const requestId = response.headers.get('X-Request-ID')
  if (!response.ok || !response.body) {
    // P5：404（会话不存在）、503（没配置大模型）在开始 SSE 之前就返回了，响应体是 { detail: "..." }
    const detail = await response
      .json()
      .then((data: { detail?: unknown }) => (typeof data.detail === 'string' ? data.detail : ''))
      .catch(() => '')
    // P6：带上状态码，调用方要区分 409（问题已经处理过了 / 还有问题没处理）
    throw Object.assign(new Error(withRequestId(detail || `请求失败（HTTP ${response.status}）`, requestId)), {
      status: response.status,
    })
  }
  const reader = response.body.getReader()

  // async function*：异步生成器，调用方用 for await 逐个取事件（相当于一个"异步的数组"）
  async function* events(): AsyncGenerator<E> {
    const decoder = new TextDecoder() // 把字节流解码成字符串（中文是多字节字符，必须用 stream 模式解码）
    let buffer = '' // 网络分块不保证正好按消息边界切开，没处理完的部分先存着
    while (true) {
      const { done, value } = await reader.read()
      if (done) return
      buffer += decoder.decode(value, { stream: true })
      // 按空行切分出完整的消息，最后一段可能不完整，留到下一轮再处理
      const parts = buffer.split('\n\n')
      buffer = parts.pop() ?? ''
      for (const part of parts) {
        // P4：": ping" 心跳行（SSE 注释）不以 "data: " 开头，在这里被跳过
        if (part.startsWith('data: ')) yield JSON.parse(part.slice('data: '.length)) as E
      }
    }
  }
  return { requestId, events: events() }
}

export async function generateTripPlanStream(
  request: TripRequest,
  handlers: StreamHandlers = {},
  signal?: AbortSignal,
): Promise<TripPlanResult> {
  const { requestId, events } = await openSse<StreamEvent>('/api/trip/plan/stream', request, signal)
  for await (const event of events) {
    switch (event.type) {
      case 'thread':
        handlers.onThread?.(event.thread_id)
        break
      case 'progress':
        handlers.onProgress?.(event.stage, event.percent)
        break
      case 'tool':
        handlers.onTool?.({ name: event.name, ok: event.ok, count: event.count })
        break
      case 'day':
        handlers.onDay?.(event.index, event.day)
        break
      case 'reset':
        handlers.onReset?.()
        break
      case 'error':
        throw new Error(withRequestId(event.message, requestId))
      case 'result':
        if (event.data.success && event.data.data) {
          return {
            plan: event.data.data,
            message: event.data.message,
            traceId: event.data.trace_id,
            threadId: event.data.thread_id,
            pending: event.data.pending,
          }
        }
        throw new Error(withRequestId(event.data.message || '生成失败', requestId))
    }
  }
  throw new Error(withRequestId('连接意外中断，没有收到结果', requestId))
}

// ============================================================
// P5：会话：恢复、对话修改、撤销
// ============================================================

/** 按 thread_id 恢复行程和对话（刷新页面、换设备打开都靠它）。不存在或不是自己的会话返回 404 */
export async function getSession(threadId: string): Promise<TripSession> {
  const { data } = await http.get<TripSession>(`/api/trip/${encodeURIComponent(threadId)}`)
  return data
}

/** 撤销上一次修改，返回值和 getSession 一样。没有可以撤销的修改时后端返回 400 */
export async function undoEdit(threadId: string): Promise<TripSession> {
  const { data } = await http.post<TripSession>(`/api/trip/${encodeURIComponent(threadId)}/undo`)
  return data
}

/** 取出错误上的 HTTP 状态码（axios 拦截器和 openSse 都会附上），没有时为 undefined */
export function httpStatus(error: unknown): number | undefined {
  return (error as Error & { status?: number }).status
}

/** 撤销失败是不是因为"没有可以撤销的修改"（400）：这种情况前端只需要把按钮置灰 */
export function isNothingToUndo(error: unknown): boolean {
  return httpStatus(error) === 400
}

type TurnEvent =
  | { type: 'token'; text: string }
  | { type: 'tool_result'; name: string; content: string }
  | { type: 'progress'; stage: string; percent: number } // P6：降档后重新生成行程，会推送和首页一样的进度
  | { type: 'interrupt' } & PendingQuestion // P6：又有新问题要确认（最终以 done 里的 pending 为准）
  | { type: 'done'; reply: string; plan: TripPlan | null; pending: PendingQuestion[] }
  | { type: 'error'; message: string }

export interface TurnHandlers {
  onToken?: (text: string) => void // 助手回复的一小段文字，逐段追加到气泡里（打字机效果）
  onToolResult?: (name: string, content: string) => void // 工具执行结果，例如"已把第 2 天的……"
  onProgress?: (stage: string) => void // 重新生成行程时的阶段文字
}

/** 一轮对话或恢复的结果：助手的回复、最新的行程、还没回答的问题 */
export interface TurnResult {
  reply: string
  plan: TripPlan | null
  pending: PendingQuestion[]
}

/** 对话和恢复两个接口的事件完全一样（后端都以 done_event 结束），共用一个解析循环 */
async function streamTurn(path: string, body: unknown, handlers: TurnHandlers, signal?: AbortSignal): Promise<TurnResult> {
  const { requestId, events } = await openSse<TurnEvent>(path, body, signal)
  for await (const event of events) {
    switch (event.type) {
      case 'token':
        handlers.onToken?.(event.text)
        break
      case 'tool_result':
        handlers.onToolResult?.(event.name, event.content)
        break
      case 'progress':
        handlers.onProgress?.(event.stage)
        break
      case 'error':
        throw new Error(withRequestId(event.message, requestId))
      case 'done':
        return { reply: event.reply, plan: event.plan, pending: event.pending }
    }
  }
  throw new Error(withRequestId('连接意外中断，没有收到结果', requestId))
}

/** 用自然语言修改行程（SSE）。还有问题没回答时后端返回 409 */
export function chatStream(threadId: string, message: string, handlers: TurnHandlers = {}, signal?: AbortSignal) {
  const body: ChatRequest = { message }
  return streamTurn(`/api/trip/${encodeURIComponent(threadId)}/chat`, body, handlers, signal)
}

/**
 * P6：回答等待确认的问题，图从暂停的地方继续（SSE）
 * 问题已经被回答过（双击、两个标签页）时后端返回 409
 */
export function resumeStream(threadId: string, body: ResumeRequest, handlers: TurnHandlers = {}, signal?: AbortSignal) {
  return streamTurn(`/api/trip/${encodeURIComponent(threadId)}/resume`, body, handlers, signal)
}

// ============================================================
// P2：用户反馈（👍 / 👎），写回 Langfuse 里对应的 trace
// ============================================================

/** 提交反馈：返回后端的结果。success=false 表示后端没启用 Langfuse，反馈没有被记录 */
export async function submitFeedback(body: FeedbackRequest): Promise<{ success: boolean; message: string }> {
  const { data } = await http.post<{ success: boolean; message: string }>('/api/trip/feedback', body)
  return data
}

// ============================================================
// P7：长期记忆（"AI 记住了我什么"）
// ============================================================

/** 当前用户（X-User-Id）的全部长期记忆 */
export async function getMemories(): Promise<MemoryItem[]> {
  const { data } = await http.get<MemoryItem[]>('/api/me/memories')
  return data
}

/** 删除一条记忆。只能删自己的：别人的 key 在自己的 namespace 里找不到，后端返回 404 */
export async function deleteMemory(key: string): Promise<void> {
  await http.delete(`/api/me/memories/${encodeURIComponent(key)}`)
}

/** 清空全部记忆，返回删掉了几条 */
export async function clearMemories(): Promise<number> {
  const { data } = await http.delete<{ success: boolean; deleted: number }>('/api/me/memories')
  return data.deleted
}
