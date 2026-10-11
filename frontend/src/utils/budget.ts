import type { Budget, TripPlan } from '@/types'

/**
 * 前端重算预算：编辑行程（删除、调整景点）之后，预算要跟着变
 * 口径必须和后端 app/agents/output.py 的 recompute_budget 完全一致：
 *   门票、餐饮 × 人数；酒店 × 房间数（2 人一间，向上取整）；交通沿用原值
 * 这是"同一套逻辑写了两遍"，作业 2 会讨论更好的做法
 */
export function recomputeBudget(plan: TripPlan, people: number): Budget {
  const rooms = Math.ceil(people / 2)
  const sum = (values: number[]) => values.reduce((a, b) => a + b, 0)

  const total_attractions = sum(plan.days.flatMap((d) => d.attractions.map((a) => a.ticket_price))) * people
  const total_meals = sum(plan.days.flatMap((d) => d.meals.map((m) => m.estimated_cost))) * people
  const total_hotels = sum(plan.days.map((d) => d.hotel?.estimated_cost ?? 0)) * rooms
  const total_transportation = plan.budget?.total_transportation ?? 0

  return {
    total_attractions,
    total_meals,
    total_hotels,
    total_transportation,
    total: total_attractions + total_meals + total_hotels + total_transportation,
  }
}
