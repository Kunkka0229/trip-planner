// 前端类型的统一入口：组件和 services 都从 '@/types' 导入，不直接碰 api.d.ts
// api.d.ts 由 `npm run gen:api` 从后端 /openapi.json 自动生成，不要手改
// 后端改了模型 → 启动后端 → npm run gen:api → npm run build，vue-tsc 会标出所有受影响的地方
import type { components } from './api'

type Schemas = components['schemas']

// Pydantic 里有默认值的字段，OpenAPI 会标成"可选"，生成的类型就带了 ?
// 对请求来说没错（前端可以不传，后端补默认值）；但响应里后端一定会输出这些字段
// 所以响应类型用 DeepRequired 递归去掉 ?（-? 只去掉"可能缺失"，Hotel | null 里的 null 会保留）
type DeepRequired<T> = T extends (infer U)[]
  ? DeepRequired<U>[]
  : T extends object
    ? { [K in keyof T]-?: DeepRequired<T[K]> }
    : T
type Resp<K extends keyof Schemas> = DeepRequired<Schemas[K]>

// ============ 请求 ============

export type TripRequest = Schemas['TripRequest']
export type PartyInfo = Schemas['PartyInfo']
export type BudgetConstraint = Schemas['BudgetConstraint']
// 从字段上取类型：后端 Literal['hard', 'soft', 'none'] → 'hard' | 'soft' | 'none'
export type BudgetStrictness = BudgetConstraint['strictness']
// P2：用户反馈的请求体 { trace_id, helpful, comment }
export type FeedbackRequest = Schemas['FeedbackRequest']

// ============ 响应（都套一层 Resp，去掉多余的 ?）============

export type Location = Resp<'Location'>
export type Attraction = Resp<'Attraction'>
export type Meal = Resp<'Meal'>
export type MealType = Meal['type']
export type Hotel = Resp<'Hotel'>
export type DayPlan = Resp<'DayPlan'>
export type WeatherInfo = Resp<'WeatherInfo'>
export type Budget = Resp<'Budget'>
export type TripPlan = Resp<'TripPlan'>
export type TripPlanResponse = Resp<'TripPlanResponse'>
export type POIInfo = Resp<'POIInfo'>

// ============ P5：会话（多轮修改） ============

export type ChatRequest = Schemas['ChatRequest']
export type ChatMessage = Resp<'ChatMessage'>
// GET /api/trip/{thread_id}、POST /api/trip/{thread_id}/undo 的返回：行程 + 对话记录
export type TripSession = Resp<'TripSession'>
