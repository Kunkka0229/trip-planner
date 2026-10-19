// P5：对话修改的状态和操作（发送、撤销、取消）；P6：等待确认的问题和回答
// 抽成 composable（相当于 React 的自定义 Hook）：状态放在结果页里，
// 桌面端的右侧面板、移动端的底部抽屉、确认框用的是同一份数据，切换屏幕宽度时对话不会丢
import { computed, onBeforeUnmount, ref, type Ref } from 'vue'
import { message } from 'ant-design-vue'
import { chatStream, getSession, httpStatus, isNothingToUndo, resumeStream, undoEdit, type TurnResult } from '@/services/api'
import type { ChatMessage, Decision, PendingQuestion, TripPlan, TripSession } from '@/types'

/** 界面上的一条消息：在后端 ChatMessage 的基础上，加上只在前端用的状态 */
export interface UiMessage extends ChatMessage {
  tools?: string[] // 这一轮调用工具的结果，显示成小标签"🔧 已把第 2 天……"
  status?: string // P6：重新生成行程时的阶段文字，例如"正在重新生成（第 1 次）"
  pending?: boolean // 助手还在生成
  error?: boolean // 这一轮失败了
}

export function useTripChat(threadId: Ref<string | undefined>, onPlanChanged: (plan: TripPlan) => void) {
  const messages = ref<UiMessage[]>([])
  const sending = ref(false)
  const undoing = ref(false)
  // 能不能撤销：先乐观地认为可以，后端返回 400（没有可撤销的修改）时再置灰
  const canUndo = ref(false)

  // ---------- P6：人工确认 ----------
  const pending = ref<PendingQuestion[]>([]) // 后端暂停着、等用户回答的问题
  const confirmOpen = ref(false) // 确认框是否打开（用户可以先关掉看行程，再从对话面板重新打开）
  const answering = ref<Decision | null>(null) // 正在提交哪个选项：按钮立刻进入 loading，防止双击
  const current = computed(() => pending.value[0] ?? null) // 一次只问一个
  // 有问题没回答时不能发新消息：后端也会拒绝（409），因为暂停的那一步会永远接不上
  const blocked = computed(() => pending.value.length > 0)

  let controller: AbortController | null = null

  function setPending(list: PendingQuestion[]) {
    pending.value = list
    confirmOpen.value = list.length > 0 // 有新问题就自动弹出
  }

  /** 用后端返回的会话覆盖本地状态（进入页面、撤销之后、409 之后重新同步） */
  function load(session: TripSession) {
    messages.value = session.messages.map((m) => ({ ...m }))
    canUndo.value = session.messages.length > 0
    setPending(session.pending)
  }

  /** 和后端重新同步：409 说明前端的状态过期了（别的标签页已经回答过） */
  async function refresh() {
    if (!threadId.value) return
    const session = await getSession(threadId.value)
    load(session)
    onPlanChanged(session.plan)
  }

  /**
   * 对话和恢复共用的"一轮"：先放一个空的助手气泡，执行请求时往里追加文字和工具结果
   * request 是真正发请求的函数，拿到 handlers 和 signal
   */
  async function runTurn(request: (handlers: Parameters<typeof chatStream>[2], signal: AbortSignal) => Promise<TurnResult>) {
    messages.value.push({ role: 'assistant', content: '', tools: [], pending: true })
    // 从数组里取出来的才是响应式代理，直接改它界面才会更新（push 进去的原始对象不是）
    const bot = messages.value[messages.value.length - 1]

    sending.value = true
    controller = new AbortController()
    try {
      const result = await request(
        {
          onToken: (t) => (bot.content += t),
          onToolResult: (_name, text) => bot.tools!.push(text),
          onProgress: (stage) => (bot.status = stage),
        },
        controller.signal,
      )
      // 逐段拼出来的文字可能包含模型调用工具之前说的话，以后端整理好的最终回复为准
      if (result.reply) bot.content = result.reply
      bot.content ||= result.pending.length ? '有一个操作需要你确认' : '已完成'
      canUndo.value = true
      if (result.plan) onPlanChanged(result.plan)
      setPending(result.pending)
    } catch (error) {
      const err = error as Error
      if (err.name === 'AbortError') {
        bot.content ||= '（已停止）'
      } else if (httpStatus(err) === 409) {
        // 409：问题已经被处理过了（双击、另一个标签页），或者还有问题没处理。以后端为准重新同步
        messages.value.pop()
        message.info('这个操作已经处理过了，已刷新为最新状态')
        await refresh().catch(() => undefined)
      } else {
        bot.error = true
        bot.content ||= err.message
        message.error(err.message)
      }
    } finally {
      bot.status = undefined
      bot.pending = false
      sending.value = false
      controller = null
    }
  }

  async function send(text: string) {
    const content = text.trim()
    const id = threadId.value
    if (!content || sending.value || blocked.value || !id) return
    messages.value.push({ role: 'user', content })
    await runTurn((handlers, signal) => chatStream(id, content, handlers, signal))
  }

  /** P6：回答当前的问题，图从暂停的地方继续 */
  async function answer(decision: Decision, reason = '') {
    const question = current.value
    const id = threadId.value
    if (!question || !id || answering.value) return // answering：已经在提交了，忽略重复点击
    answering.value = decision
    try {
      await runTurn((handlers, signal) =>
        resumeStream(id, { interrupt_id: question.id, decision, message: reason }, handlers, signal),
      )
    } finally {
      answering.value = null
    }
  }

  /** 停止生成：中断 fetch，后端发现连接断开后取消任务（和 P4 首页的"取消"一样） */
  function stop() {
    controller?.abort()
  }

  async function undo() {
    if (!threadId.value || undoing.value) return
    undoing.value = true
    try {
      const session = await undoEdit(threadId.value)
      load(session)
      onPlanChanged(session.plan)
      message.success('已撤销上一次修改')
    } catch (error) {
      if (isNothingToUndo(error)) {
        canUndo.value = false
        message.info('没有可以撤销的修改')
      } else {
        message.error((error as Error).message)
      }
    } finally {
      undoing.value = false
    }
  }

  // 离开结果页时停止还在进行的生成
  onBeforeUnmount(stop)

  return {
    messages,
    sending,
    undoing,
    canUndo,
    pending,
    current,
    blocked,
    confirmOpen,
    answering,
    load,
    send,
    answer,
    stop,
    undo,
  }
}

export type TripChat = ReturnType<typeof useTripChat>
