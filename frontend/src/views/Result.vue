<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import TripMap from '@/components/TripMap.vue'
import type { TripPlan, TripRequest, WeatherInfo } from '@/types'
import { recomputeBudget } from '@/utils/budget'
import { exportAsImage, exportAsPdf } from '@/utils/export'

const router = useRouter()

// ---------- 读取首页存进来的数据 ----------
// 泛型函数：读取并解析 JSON，失败返回 null
function load<T>(key: string): T | null {
  try {
    const raw = sessionStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : null
  } catch {
    return null
  }
}

const plan = ref<TripPlan | null>(load<TripPlan>('tripPlan'))
const request = load<TripRequest>('tripRequest')
const notice = sessionStorage.getItem('tripMessage') ?? ''
// 总人数：前端没有 computed_field，自己加一下
const people = request ? request.party.adults + request.party.children + request.party.infants : 1

const mealLabels = { breakfast: '早餐', lunch: '午餐', dinner: '晚餐', snack: '小吃' } as const

// ---------- 天气（第 10 课作业 1） ----------
// 高德只能预报今天起 4 天，更远的日期后端会给 "暂无预报"，温度是字符串 "未知"（第 9 课 align_weather）
function hasForecast(w: WeatherInfo): boolean {
  return typeof w.day_temp === 'number' && typeof w.night_temp === 'number'
}

// 按天气描述的关键词选图标；顺序有讲究："雷阵雨" 要先于 "雨" 判断，"雨夹雪" 要先于 "雪"
const WEATHER_ICONS: [string, string][] = [
  ['雷', '⛈️'],
  ['雨夹雪', '🌨️'],
  ['雪', '❄️'],
  ['雨', '🌧️'],
  ['雾', '🌫️'],
  ['霾', '🌫️'],
  ['沙', '🌪️'],
  ['尘', '🌪️'],
  ['阴', '☁️'],
  ['多云', '⛅'],
  ['晴', '☀️'],
]

function weatherIcon(text: string): string {
  return WEATHER_ICONS.find(([keyword]) => text.includes(keyword))?.[1] ?? '—'
}

// ---------- 侧边锚点导航（书 13.6.5） ----------
// a-anchor 会自动监听滚动、高亮当前区块，点击时平滑滚动到对应 id 的元素
const anchorItems = computed(() => [
  { key: 'overview', href: '#overview', title: '概览' },
  { key: 'budget', href: '#budget', title: '预算' },
  { key: 'weather', href: '#weather', title: '天气' },
  { key: 'map', href: '#map', title: '地图' },
  ...(plan.value?.days ?? []).map((d) => ({
    key: `day-${d.day_index}`,
    href: `#day-${d.day_index}`,
    title: `第 ${d.day_index + 1} 天`,
  })),
])

// 地图上显示哪一天：-1 = 全部
// 不用 null 表示"全部"：a-segmented 的 value 只接受 string | number，传 null 会有警告
const activeDay = ref(-1)

// ---------- 编辑模式（书 13.6.3） ----------
const editing = ref(false)
let backup: TripPlan | null = null // 进入编辑时的快照，用于"取消"

function startEdit() {
  // 深拷贝：JSON 往返一次，得到和响应式对象完全无关的普通对象
  // （直接 backup = plan.value 只是复制了引用，后面的编辑会把备份一起改掉）
  backup = JSON.parse(JSON.stringify(plan.value))
  editing.value = true
}

function cancelEdit() {
  plan.value = backup
  editing.value = false
  message.info('已撤销修改')
}

function saveEdit() {
  if (!plan.value) return
  plan.value.budget = recomputeBudget(plan.value, people) // 景点变了，门票预算也要跟着变
  sessionStorage.setItem('tripPlan', JSON.stringify(plan.value)) // 刷新后修改依然保留
  editing.value = false
  message.success('修改已保存')
}

function moveAttraction(dayIndex: number, index: number, step: -1 | 1) {
  const list = plan.value!.days[dayIndex].attractions // !：非空断言，编辑模式下 plan 一定存在
  const target = index + step
  if (target < 0 || target >= list.length) return
  // 解构赋值交换两个元素，不需要临时变量
  ;[list[index], list[target]] = [list[target], list[index]]
}

function removeAttraction(dayIndex: number, index: number) {
  const list = plan.value!.days[dayIndex].attractions
  if (list.length <= 1) {
    message.warning('每天至少保留一个景点')
    return
  }
  list.splice(index, 1)
}

// ---------- 导出（书 13.6.4） ----------
const exportRef = ref<HTMLElement>() // 要导出的区域
const exporting = ref(false)

async function handleExport(type: 'image' | 'pdf') {
  if (!exportRef.value || !plan.value) return
  exporting.value = true
  const filename = `${plan.value.city}-${plan.value.days.length}日游`
  try {
    if (type === 'image') await exportAsImage(exportRef.value, filename)
    else await exportAsPdf(exportRef.value, filename)
  } catch (e) {
    message.error(`导出失败: ${String(e)}`)
  } finally {
    exporting.value = false
  }
}
</script>

<template>
  <a-result v-if="!plan" status="warning" title="没有找到行程数据">
    <template #extra>
      <a-button type="primary" @click="router.push({ name: 'home' })">返回首页</a-button>
    </template>
  </a-result>

  <a-row v-else :gutter="24">
    <!-- 左侧导航：小屏幕隐藏（xs=0） -->
    <a-col :xs="0" :md="5">
      <a-anchor :items="anchorItems" :offset-top="16" />
    </a-col>

    <a-col :xs="24" :md="19">
      <!-- 工具栏：data-export-ignore 表示导出时忽略这部分 -->
      <a-space style="margin-bottom: 16px" wrap data-export-ignore>
        <a-button @click="router.push({ name: 'home' })">重新规划</a-button>
        <template v-if="!editing">
          <a-button @click="startEdit">编辑行程</a-button>
          <a-button :loading="exporting" @click="handleExport('image')">导出图片</a-button>
          <a-button :loading="exporting" @click="handleExport('pdf')">导出 PDF</a-button>
        </template>
        <template v-else>
          <a-button type="primary" @click="saveEdit">保存</a-button>
          <a-button @click="cancelEdit">取消</a-button>
        </template>
      </a-space>

      <div ref="exportRef">
        <!-- 概览 -->
        <a-card id="overview" class="section">
          <h2>{{ plan.city }} {{ plan.days.length }} 日游</h2>
          <p>{{ plan.start_date }} 至 {{ plan.end_date }} · {{ people }} 人</p>
          <a-alert v-if="notice" :message="notice" type="info" show-icon style="margin-bottom: 12px" />
          <p>{{ plan.overall_suggestions }}</p>
        </a-card>

        <!-- 预算（书 13.6.1） -->
        <a-card v-if="plan.budget" id="budget" title="预算" class="section">
          <a-row :gutter="16">
            <a-col :span="12" :md="4"><a-statistic title="门票" :value="plan.budget.total_attractions" /></a-col>
            <a-col :span="12" :md="5"><a-statistic title="住宿" :value="plan.budget.total_hotels" /></a-col>
            <a-col :span="12" :md="5"><a-statistic title="餐饮" :value="plan.budget.total_meals" /></a-col>
            <a-col :span="12" :md="5"><a-statistic title="交通" :value="plan.budget.total_transportation" /></a-col>
            <a-col :span="24" :md="5"><a-statistic title="合计（元）" :value="plan.budget.total" :value-style="{ color: '#1677ff' }" /></a-col>
          </a-row>
          <p v-if="request?.budget_constraint?.amount" style="margin-top: 12px">
            预算上限 {{ request.budget_constraint.amount }} 元，
            <a-tag :color="plan.budget.total <= request.budget_constraint.amount ? 'green' : 'red'">
              {{ plan.budget.total <= request.budget_constraint.amount ? '未超预算' : '已超预算' }}
            </a-tag>
          </p>
        </a-card>

        <!-- 天气 -->
        <a-card id="weather" title="天气" class="section">
          <a-row :gutter="[12, 12]">
            <a-col v-for="w in plan.weather_info" :key="w.date" :xs="12" :md="6">
              <a-card size="small" :class="{ 'no-forecast': !hasForecast(w) }">
                <div>{{ w.date }}</div>
                <div class="weather-text">{{ weatherIcon(w.day_weather) }} {{ w.day_weather }}</div>
                <!-- 远期天气后端给的是 "未知"，不显示成 "未知° ~ 未知°" -->
                <div v-if="hasForecast(w)">{{ w.night_temp }}° ~ {{ w.day_temp }}°</div>
                <div v-else class="weather-hint">— · 出发前 3 天内可查看</div>
              </a-card>
            </a-col>
          </a-row>
        </a-card>

        <!-- 地图 -->
        <a-card id="map" title="地图" class="section">
          <template #extra>
            <a-segmented
              v-model:value="activeDay"
              :options="[{ label: '全部', value: -1 }, ...plan.days.map((d) => ({ label: `第${d.day_index + 1}天`, value: d.day_index }))]"
              data-export-ignore
            />
          </template>
          <TripMap :days="plan.days" :active-day="activeDay" />
        </a-card>

        <!-- 每日行程 -->
        <a-card
          v-for="(day, dayIndex) in plan.days"
          :id="`day-${day.day_index}`"
          :key="day.date"
          :title="`第 ${day.day_index + 1} 天 · ${day.date}`"
          class="section"
        >
          <p>{{ day.description }}</p>
          <p v-if="day.hotel">🏨 {{ day.hotel.name }}（{{ day.hotel.estimated_cost }} 元/间/晚）</p>

          <a-list :data-source="day.attractions" size="small" bordered>
            <template #renderItem="{ item, index }">
              <a-list-item>
                <a-list-item-meta :title="`${index + 1}. ${item.name}`" :description="`${item.address} · 约 ${item.visit_duration} 分钟 · 门票 ${item.ticket_price} 元`" />
                <!-- 编辑模式下才显示操作按钮 -->
                <template v-if="editing" #actions>
                  <a-button size="small" :disabled="index === 0" @click="moveAttraction(dayIndex, index, -1)">上移</a-button>
                  <a-button size="small" :disabled="index === day.attractions.length - 1" @click="moveAttraction(dayIndex, index, 1)">下移</a-button>
                  <a-button size="small" danger @click="removeAttraction(dayIndex, index)">删除</a-button>
                </template>
              </a-list-item>
            </template>
          </a-list>

          <p style="margin-top: 12px">
            🍽️ {{ day.meals.map((m) => `${mealLabels[m.type]}·${m.name}（${m.estimated_cost} 元/人）`).join('、') }}
          </p>
        </a-card>
      </div>
    </a-col>
  </a-row>
</template>

<style scoped>
.section {
  margin-bottom: 16px;
  scroll-margin-top: 16px; /* 锚点跳转时顶部留出一点空间 */
}

.weather-text {
  font-size: 18px;
  font-weight: 600;
}

/* 没有预报的日期：整体变灰，和有真实数据的日期区分开 */
.no-forecast {
  color: rgba(0, 0, 0, 0.45);
  background: #fafafa;
}

.weather-hint {
  font-size: 12px;
}
</style>
