import axios, { AxiosError } from 'axios'
import type { TripPlan, TripPlanResponse, TripRequest } from '@/types'

// 读取 .env 里的 VITE_API_BASE_URL（只有 VITE_ 开头的变量才会暴露给前端代码）
// 留空 → baseURL 为 ''，请求发到当前域名的 /api/...，由 Vite 代理转发
const baseURL = (import.meta.env.VITE_API_BASE_URL ?? '').trim().replace(/\/$/, '')

const http = axios.create({
  baseURL,
  // 大模型生成行程可能要 1~3 分钟，axios 默认不超时，这里给 5 分钟上限
  timeout: 5 * 60 * 1000,
  headers: { 'Content-Type': 'application/json' },
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

  const detail = err.response.data?.detail
  // 422：detail 是数组；去掉 loc 第一项 'body'，拼成 "party.adults: Input should be ..."
  if (Array.isArray(detail)) {
    return detail.map((d) => `${d.loc.slice(1).join('.') || '请求'}: ${d.msg}`).join('；')
  }
  // HTTPException：detail 是字符串
  return detail || `请求失败（HTTP ${err.response.status}）`
}

// 响应拦截器：失败时统一转换错误信息，业务代码只需要 catch 后显示 error.message
http.interceptors.response.use(
  (response) => response,
  (error) => Promise.reject(new Error(toErrorMessage(error))),
)

/** 生成旅行计划：成功返回行程和后端的提示信息，失败抛出 Error */
export async function generateTripPlan(request: TripRequest): Promise<{ plan: TripPlan; message: string }> {
  const { data } = await http.post<TripPlanResponse>('/api/trip/plan', request)
  // HTTP 200 但业务失败（success=false）也当成错误抛出
  if (!data.success || !data.data) {
    throw new Error(data.message || '生成失败')
  }
  // 第 10 课：把 message 一起返回。兜底方案、grounding 警告都写在 message 里（第 7、9 课）
  return { plan: data.data, message: data.message }
}

// ============================================================
// 第 11 课：SSE 流式接口，接收后端推送的真实进度
// ============================================================

type StreamEvent =
  | { type: 'progress'; stage: string; percent: number }
  | { type: 'result'; data: TripPlanResponse }
  | { type: 'error'; message: string }

/**
 * 为什么不用浏览器自带的 EventSource？它只支持 GET 请求，而我们要 POST 一个请求体
 * 所以用 fetch 拿到响应流，自己按 SSE 格式解析：每条消息是 "data: {...}\n\n"
 */
export async function generateTripPlanStream(
  request: TripRequest,
  onProgress: (stage: string, percent: number) => void,
): Promise<{ plan: TripPlan; message: string }> {
  const response = await fetch(`${baseURL}/api/trip/plan/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
  if (!response.ok || !response.body) {
    throw new Error(`请求失败（HTTP ${response.status}）`)
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder() // 把字节流解码成字符串（中文是多字节字符，必须用 stream 模式解码）
  let buffer = '' // 网络分块不保证正好按消息边界切开，没处理完的部分先存着

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })

    // 按空行切分出完整的消息，最后一段可能不完整，留到下一轮再处理
    const parts = buffer.split('\n\n')
    buffer = parts.pop() ?? ''

    for (const part of parts) {
      if (!part.startsWith('data: ')) continue
      const event = JSON.parse(part.slice('data: '.length)) as StreamEvent
      if (event.type === 'progress') {
        onProgress(event.stage, event.percent)
      } else if (event.type === 'error') {
        throw new Error(event.message)
      } else if (event.data.success && event.data.data) {
        return { plan: event.data.data, message: event.data.message }
      } else {
        throw new Error(event.data.message || '生成失败')
      }
    }
  }
  throw new Error('连接意外中断，没有收到结果')
}
