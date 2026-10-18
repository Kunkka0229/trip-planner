<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Grid, message } from 'ant-design-vue'
import ChatPanel from '@/components/ChatPanel.vue'
import TripMap from '@/components/TripMap.vue'
import { useTripChat } from '@/composables/useTripChat'
import { getSession, submitFeedback } from '@/services/api'
import type { TripPlan, TripRequest, WeatherInfo } from '@/types'
import { recomputeBudget } from '@/utils/budget'
import { exportAsImage, exportAsPdf } from '@/utils/export'

const router = useRouter()
const route = useRoute()

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

// P5：路由参数里的 thread_id（/result/:threadId?）。params 的类型是 string | string[]，只认字符串
const threadId = computed(() => (typeof route.params.threadId === 'string' ? route.params.threadId : undefined))

const plan = ref<TripPlan | null>(null)
const loading = ref(false)
const loadError = ref('') // 会话不存在、不是自己的（都是 404）、网络错误……

// 请求只在首页提交时存过一份；换设备打开时没有，人数、预算上限就不显示
const request = load<TripRequest>('tripRequest')
// 首页的提示（兜底、grounding 警告）只属于刚生成的那个会话，打开别的会话时不显示
const notice = computed(() =>
  (sessionStorage.getItem('tripMessageThread') ?? '') === (threadId.value ?? '')
    ? (sessionStorage.getItem('tripMessage') ?? '')
    : '',
)
// 总人数：前端没有 computed_field，自己加一下
const people = request ? request.party.adults + request.party.children + request.party.infants : 1

// ---------- P5：对话修改 ----------
const highlightDays = ref(new Set<number>()) // 刚被修改的天，卡片高亮闪一下

/** 对话或撤销返回了新行程：找出哪几天变了，替换数据，高亮并滚动到第一处变化 */
function applyPlan(next: TripPlan) {
  const prev = plan.value
  const changed = next.days
    .map((_, i) => i)
    .filter((i) => JSON.stringify(next.days[i]) !== JSON.stringify(prev?.days[i]))
  plan.value = next
  highlightDays.value = new Set(changed)
  setTimeout(() => (highlightDays.value = new Set()), 2000) // 和 CSS 动画时长一致
  if (changed.length) {
    document.getElementById(`day-${next.days[changed[0]].day_index}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}

const chat = useTripChat(threadId, applyPlan)

// 宽屏（md 及以上）对话面板放在右侧；窄屏收进底部抽屉，点悬浮按钮打开
const screens = Grid.useBreakpoint()
const isWide = computed(() => !!screens.value.md)
const drawerOpen = ref(false)

// 进入页面、或者从一个会话跳到另一个会话时加载数据
// 有 thread_id：从后端恢复行程和对话（刷新、换设备打开都能用，前提是同一个 X-User-Id）
// 没有 thread_id：mock 数据，退回读 sessionStorage（和 P4 之前一样）
watch(
  threadId,
  async (id) => {
    loadError.value = ''
    if (!id) {
      plan.value = load<TripPlan>('tripPlan')
      return
    }
    loading.value = true
    try {
      const session = await getSession(id)
      plan.value = session.plan
      chat.load(session)
    } catch (e) {
      plan.value = null
      loadError.value = (e as Error).message
    } finally {
      loading.value = false
    }
  },
  { immediate: true },
)

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

// ---------- P2：用户反馈（👍 / 👎） ----------
// 首页存进来的 trace_id；没启用 Langfuse 时没有，反馈按钮就不显示
const traceId = sessionStorage.getItem('tripTraceId')
// 已经反馈过的 trace_id 记在 sessionStorage 里：刷新页面后不会让用户重复提交
const FEEDBACK_DONE_KEY = 'tripFeedbackDone'
const feedbackDone = ref(traceId !== null && sessionStorage.getItem(FEEDBACK_DONE_KEY) === traceId)
const feedbackSubmitting = ref(false)
const dislikeOpen = ref(false) // 👎 的原因输入框是否打开
const dislikeComment = ref('')

async function sendFeedback(helpful: boolean, comment = '') {
  if (!traceId) return
  feedbackSubmitting.value = true
  try {
    const result = await submitFeedback({ trace_id: traceId, helpful, comment })
    if (result.success) {
      feedbackDone.value = true
      sessionStorage.setItem(FEEDBACK_DONE_KEY, traceId)
      dislikeOpen.value = false
      message.success('感谢你的反馈')
    } else {
      message.info(result.message) // 后端没启用 Langfuse 时 success=false
    }
  } catch (e) {
    message.error((e as Error).message) // api.ts 已经附上了 request_id
  } finally {
    feedbackSubmitting.value = false
  }
}

function openDislike() {
  dislikeComment.value = ''
  dislikeOpen.value = true
}
</script>

<template>
  <div v-if="loading" class="page-loading"><a-spin tip="正在加载行程..." /></div>

  <a-result v-else-if="!plan" status="warning" :title="loadError || '没有找到行程数据'">
    <template #extra>
      <a-button type="primary" @click="router.push({ name: 'home' })">返回首页</a-button>
    </template>
  </a-result>

  <a-row v-else :gutter="24">
    <!-- 左侧导航：小屏幕隐藏（xs=0）；有对话面板时只在很宽的屏幕（xl）显示，给行程和对话腾地方 -->
    <a-col :xs="0" :md="threadId ? 0 : 5" :xl="threadId ? 4 : 5">
      <a-anchor :items="anchorItems" :offset-top="16" />
    </a-col>

    <a-col :xs="24" :md="threadId ? 14 : 19" :xl="threadId ? 12 : 19">
      <!-- 工具栏：data-export-ignore 表示导出时忽略这部分 -->
      <a-space style="margin-bottom: 16px" wrap data-export-ignore>
        <a-button @click="router.push({ name: 'home' })">重新规划</a-button>
        <template v-if="!editing">
          <!-- P5：有会话时行程以后端为准，手动编辑只存在本地，下一次对话或撤销就会被覆盖，所以改用对话修改 -->
          <a-button v-if="!threadId" @click="startEdit">编辑行程</a-button>
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
          <p>{{ plan.start_date }} 至 {{ plan.end_date }}<template v-if="request"> · {{ people }} 人</template></p>
          <a-alert v-if="notice" :message="notice" type="info" show-icon style="margin-bottom: 12px" />
          <p>{{ plan.overall_suggestions }}</p>

          <!-- P2：反馈。没有 trace_id（没启用 Langfuse）时不显示；导出图片 / PDF 时忽略 -->
          <div v-if="traceId" class="feedback" data-export-ignore>
            <span v-if="feedbackDone" class="feedback-done">✅ 已收到你的反馈，谢谢！</span>
            <template v-else>
              <span>这份行程对你有帮助吗？</span>
              <a-button size="small" :loading="feedbackSubmitting" @click="sendFeedback(true)">👍 有帮助</a-button>
              <a-button size="small" :disabled="feedbackSubmitting" @click="openDislike">👎 没帮助</a-button>
            </template>
          </div>
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
          :class="['section', { flash: highlightDays.has(dayIndex) }]"
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

    <!-- P5：右侧对话面板（宽屏）。sticky：滚动行程时面板固定在视口里 -->
    <a-col v-if="threadId && isWide" :md="10" :xl="8">
      <a-card class="chat-card" :body-style="{ height: '100%', padding: '16px' }">
        <ChatPanel :chat="chat" />
      </a-card>
    </a-col>
  </a-row>

  <!-- P5：窄屏时对话面板收进底部抽屉 -->
  <template v-if="plan && threadId && !isWide">
    <a-float-button type="primary" tooltip="对话修改行程" @click="drawerOpen = true">
      <template #icon>💬</template>
    </a-float-button>
    <a-drawer v-model:open="drawerOpen" placement="bottom" height="75vh" :closable="false" :body-style="{ padding: '16px' }">
      <ChatPanel :chat="chat" />
    </a-drawer>
  </template>

  <!-- P2：点 👎 时收集原因。原因可以不填，但填了对改进最有用 -->
  <a-modal
    v-model:open="dislikeOpen"
    title="哪里不满意？"
    ok-text="提交"
    cancel-text="取消"
    :confirm-loading="feedbackSubmitting"
    @ok="sendFeedback(false, dislikeComment.trim())"
  >
    <a-textarea
      v-model:value="dislikeComment"
      :maxlength="500"
      show-count
      :rows="4"
      placeholder="比如：第三天是空的、景点太赶、预算超了……（选填）"
    />
  </a-modal>
</template>

<style scoped>
.page-loading {
  display: flex;
  justify-content: center;
  padding: 80px 0;
}

.chat-card {
  position: sticky;
  top: 16px;
  height: calc(100vh - 64px - 48px - 16px); /* 视口高度 - 顶栏 - 内容区上下内边距 - 留白 */
}

/* 被对话修改过的那一天：背景闪一下 */
.flash {
  animation: flash 2s ease-out;
}

@keyframes flash {
  0%,
  30% {
    background: #e6f4ff;
    box-shadow: 0 0 0 2px #91caff;
  }
  100% {
    background: #fff;
    box-shadow: none;
  }
}

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

.feedback {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px dashed #f0f0f0;
}

.feedback-done {
  color: rgba(0, 0, 0, 0.45);
}
</style>
