<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import type { Dayjs } from 'dayjs'
import { generateTripPlanStream, type DraftDay, type ToolEvent } from '@/services/api'
import type { BudgetStrictness, TripRequest } from '@/types'

const router = useRouter()

// 表单状态和接口请求体分开：
// 表单里的日期是 Dayjs 对象（日期组件需要），提交时再转成 'YYYY-MM-DD' 字符串
const form = reactive({
  city: '',
  dates: null as [Dayjs, Dayjs] | null, // a-range-picker 的值：[开始, 结束]
  transportation: '公共交通',
  accommodation: '经济型酒店',
  preferences: [] as string[],
  freeText: '',
  adults: 2,
  children: 0,
  infants: 0,
  budgetAmount: null as number | null, // null = 不限预算
  strictness: 'soft' as BudgetStrictness,
})

const transportationOptions = ['公共交通', '打车/网约车', '自驾', '骑行/步行']
const accommodationOptions = ['经济型酒店', '舒适型酒店', '高端酒店', '民宿']
const preferenceOptions = ['历史文化', '自然风光', '美食', '购物', '艺术', '亲子友好']

// 天数由日期计算得出，用户不用填，也就不会和日期对不上（后端 model_validator 会校验）
const travelDays = computed(() => {
  if (!form.dates) return 0
  const [start, end] = form.dates
  return end.diff(start, 'day') + 1
})

// ---------- 进度条（第 11 课：由后端通过 SSE 推送真实进度） ----------
const loading = ref(false)
const progress = ref(0)
const progressText = ref('')

// ---------- P4：工具清单 + 逐天草稿 + 取消 ----------
const TOOL_LABELS: Record<string, string> = { attractions: '景点', hotels: '酒店', foods: '餐厅', weather: '天气' }
const MEAL_LABELS: Record<string, string> = { breakfast: '早餐', lunch: '午餐', dinner: '晚餐', snack: '小吃' }
const tools = ref<ToolEvent[]>([])
// 用下标存：后端按 index 推送，重复推送同一天时直接覆盖
const draftDays = ref<DraftDay[]>([])
// 每次生成创建一个新的 AbortController；abort() 会中断 fetch，后端随之取消规划任务
let controller: AbortController | null = null

function toolText(tool: ToolEvent): string {
  const label = TOOL_LABELS[tool.name] ?? tool.name
  if (!tool.ok) return `❌ ${label}获取失败`
  return tool.count === undefined ? `✅ ${label}` : `✅ ${label} ${tool.count} 个`
}

function cancelGenerate() {
  controller?.abort()
}

// 离开首页（比如点了别的路由）时也中止，避免后端白白继续生成
onBeforeUnmount(cancelGenerate)

// a-form 校验通过后才会触发 @finish
async function handleSubmit() {
  if (!form.dates) {
    message.error('请选择出行日期')
    return
  }
  const [start, end] = form.dates

  // 组装成和后端 TripRequest 完全一致的结构
  const request: TripRequest = {
    city: form.city.trim(),
    start_date: start.format('YYYY-MM-DD'),
    end_date: end.format('YYYY-MM-DD'),
    travel_days: travelDays.value,
    transportation: form.transportation,
    accommodation: form.accommodation,
    preferences: form.preferences,
    free_text_input: form.freeText,
    party: { adults: form.adults, children: form.children, infants: form.infants },
    budget_constraint: {
      amount: form.budgetAmount,
      // 没填预算时，严格程度没有意义，固定为 none
      strictness: form.budgetAmount === null ? 'none' : form.strictness,
    },
  }

  loading.value = true
  progress.value = 0
  progressText.value = '正在连接...'
  tools.value = []
  draftDays.value = []
  controller = new AbortController()
  try {
    // P4：按事件类型分别处理：进度条、工具清单、逐天草稿
    const result = await generateTripPlanStream(
      request,
      {
        onProgress: (stage, percent) => {
          progressText.value = stage
          progress.value = percent
        },
        onTool: (tool) => tools.value.push(tool),
        onDay: (index, day) => {
          draftDays.value[index] = day
        },
        // 模型输出不合格、正在重试：上一次的草稿作废
        onReset: () => {
          draftDays.value = []
        },
      },
      controller.signal,
    )
    // 用 sessionStorage 把结果带到结果页：刷新结果页数据还在，关掉标签页就清空
    sessionStorage.setItem('tripPlan', JSON.stringify(result.plan))
    // 第 10 课：请求也存一份，结果页编辑行程后重算预算要用到人数
    sessionStorage.setItem('tripRequest', JSON.stringify(request))
    sessionStorage.setItem('tripMessage', result.message)
    // P2：结果页提交反馈时要用 trace_id；没启用 Langfuse 时是 null，删掉旧值，结果页就不显示反馈按钮
    if (result.traceId) {
      sessionStorage.setItem('tripTraceId', result.traceId)
    } else {
      sessionStorage.removeItem('tripTraceId')
    }
    // P5：记下这条提示属于哪个会话，结果页只在同一个会话里显示它
    sessionStorage.setItem('tripMessageThread', result.threadId ?? '')
    // 后端返回的是兜底方案（第 7 课），或者带有警告（第 9 课）时，用黄色提示
    // P6：生成后暂停等待确认（比如超预算）：结果页会根据会话里的 pending 弹出确认框
    if (result.pending.length) {
      message.info('行程已生成，有问题需要你确认')
    } else if (result.message.includes('基础行程') || result.message.includes('注意')) {
      message.warning(result.message, 6)
    } else {
      message.success('行程生成成功')
    }
    // P5：带上 thread_id 跳转，结果页从后端恢复数据；mock 数据没有 thread_id，结果页退回读 sessionStorage
    router.push({ name: 'result', params: result.threadId ? { threadId: result.threadId } : {} })
  } catch (error) {
    // 用户主动取消时 fetch 抛 AbortError，不算错误
    if ((error as Error).name === 'AbortError') {
      message.info('已取消生成')
      return
    }
    // api.ts 的拦截器已经把错误转成了可读的中文
    message.error((error as Error).message)
  } finally {
    loading.value = false // 不再需要 clearInterval：没有定时器了
    controller = null
  }
}
</script>

<template>
  <a-card title="规划你的旅行">
    <!-- layout="vertical"：标签在输入框上方；@finish：校验通过后的提交回调 -->
    <a-form :model="form" layout="vertical" @finish="handleSubmit">
      <a-row :gutter="16">
        <a-col :xs="24" :md="12">
          <!-- name 要和 form 里的字段名一致，rules 才能找到要校验的值 -->
          <a-form-item label="目的地城市" name="city" :rules="[{ required: true, whitespace: true, message: '请输入城市' }]">
            <a-input v-model:value="form.city" placeholder="例如：北京" />
          </a-form-item>
        </a-col>
        <a-col :xs="24" :md="12">
          <a-form-item label="出行日期" name="dates" :rules="[{ required: true, message: '请选择日期' }]">
            <a-range-picker v-model:value="form.dates" style="width: 100%" />
          </a-form-item>
        </a-col>
      </a-row>

      <p v-if="travelDays > 0">共 {{ travelDays }} 天</p>

      <!-- 人数的上下限和后端 PartyInfo 的 ge/le 保持一致 -->
      <a-row :gutter="16">
        <a-col :span="8">
          <a-form-item label="成人">
            <a-input-number v-model:value="form.adults" :min="1" :max="10" style="width: 100%" />
          </a-form-item>
        </a-col>
        <a-col :span="8">
          <a-form-item label="儿童">
            <a-input-number v-model:value="form.children" :min="0" :max="10" style="width: 100%" />
          </a-form-item>
        </a-col>
        <a-col :span="8">
          <a-form-item label="婴儿">
            <a-input-number v-model:value="form.infants" :min="0" :max="10" style="width: 100%" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-row :gutter="16">
        <a-col :xs="24" :md="12">
          <a-form-item label="交通方式">
            <a-select v-model:value="form.transportation" :options="transportationOptions.map((v) => ({ label: v, value: v }))" />
          </a-form-item>
        </a-col>
        <a-col :xs="24" :md="12">
          <a-form-item label="住宿偏好">
            <a-select v-model:value="form.accommodation" :options="accommodationOptions.map((v) => ({ label: v, value: v }))" />
          </a-form-item>
        </a-col>
      </a-row>

      <a-row :gutter="16">
        <a-col :xs="24" :md="12">
          <a-form-item label="总预算（元，可不填）">
            <a-input-number v-model:value="form.budgetAmount" :min="0" :step="500" style="width: 100%" />
          </a-form-item>
        </a-col>
        <a-col :xs="24" :md="12">
          <a-form-item label="预算严格程度">
            <a-radio-group v-model:value="form.strictness" :disabled="form.budgetAmount === null">
              <a-radio value="hard">不能超</a-radio>
              <a-radio value="soft">尽量不超</a-radio>
            </a-radio-group>
          </a-form-item>
        </a-col>
      </a-row>

      <a-form-item label="旅行偏好">
        <a-checkbox-group v-model:value="form.preferences" :options="preferenceOptions" />
      </a-form-item>

      <a-form-item label="额外要求">
        <a-textarea v-model:value="form.freeText" :rows="3" placeholder="例如：希望多安排一些博物馆" />
      </a-form-item>

      <!-- html-type="submit"：点击时触发表单校验 → 通过后触发 @finish -->
      <a-button type="primary" html-type="submit" size="large" block :loading="loading">生成行程</a-button>

      <div v-if="loading" style="margin-top: 16px">
        <a-progress :percent="progress" status="active" />
        <div class="progress-line">
          <span>{{ progressText }}</span>
          <a-button size="small" danger @click="cancelGenerate">取消</a-button>
        </div>

        <!-- P4：高德工具清单 -->
        <div v-if="tools.length" class="tool-list">
          <span v-for="tool in tools" :key="tool.name">{{ toolText(tool) }}</span>
        </div>

        <!-- P4：逐天草稿卡片（未校验，灰色显示） -->
        <template v-for="(day, index) in draftDays" :key="index">
          <a-card v-if="day" size="small" class="draft-card">
            <template #title>
              第 {{ index + 1 }} 天 <span v-if="day.date" class="draft-date">{{ day.date }}</span>
            </template>
            <template #extra><a-tag>草稿</a-tag></template>
            <p v-if="day.description">{{ day.description }}</p>
            <p v-if="day.attractions?.length">
              景点：{{ day.attractions.map((a) => a.name).filter(Boolean).join(' → ') }}
            </p>
            <p v-if="day.meals?.length">
              餐饮：{{ day.meals.map((m) => `${MEAL_LABELS[m.type ?? ''] ?? m.type ?? ''} ${m.name ?? ''}`).join('，') }}
            </p>
            <p v-if="day.hotel?.name">住宿：{{ day.hotel.name }}</p>
          </a-card>
        </template>
      </div>
    </a-form>
  </a-card>
</template>

<style scoped>
.progress-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 8px 0;
}
.tool-list {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 12px;
}
.draft-card {
  margin-top: 12px;
  opacity: 0.6; /* 灰色：提示这是还没校验的草稿 */
  background: #fafafa;
}
.draft-card p {
  margin-bottom: 4px;
}
.draft-date {
  font-weight: normal;
  color: #999;
  margin-left: 8px;
}
</style>
