// P5：对话修改的状态和操作（发送、撤销、取消）
// 抽成 composable（相当于 React 的自定义 Hook）：状态放在结果页里，
// 桌面端的右侧面板和移动端的底部抽屉用的是同一份数据，切换屏幕宽度时对话不会丢
import { onBeforeUnmount, ref, type Ref } from 'vue'
import { message } from 'ant-design-vue'
import { chatStream, isNothingToUndo, undoEdit } from '@/services/api'
import type { ChatMessage, TripPlan, TripSession } from '@/types'

/** 界面上的一条消息：在后端 ChatMessage 的基础上，加上只在前端用的状态 */
export interface UiMessage extends ChatMessage {
  tools?: string[] // 这一轮调用工具的结果，显示成小标签"🔧 已把第 2 天……"
  pending?: boolean // 助手还在生成
  error?: boolean // 这一轮失败了
}

export function useTripChat(threadId: Ref<string | undefined>, onPlanChanged: (plan: TripPlan) => void) {
  const messages = ref<UiMessage[]>([])
  const sending = ref(false)
  const undoing = ref(false)
  // 能不能撤销：先乐观地认为可以，后端返回 400（没有可撤销的修改）时再置灰
  const canUndo = ref(false)
  let controller: AbortController | null = null

  /** 用后端返回的会话覆盖本地对话（进入页面、撤销之后） */
  function load(session: TripSession) {
    messages.value = session.messages.map((m) => ({ ...m }))
    canUndo.value = session.messages.length > 0
  }

  async function send(text: string) {
    const content = text.trim()
    if (!content || sending.value || !threadId.value) return

    messages.value.push({ role: 'user', content })
    messages.value.push({ role: 'assistant', content: '', tools: [], pending: true })
    // 从数组里取出来的才是响应式代理，直接改它界面才会更新（push 进去的原始对象不是）
    const bot = messages.value[messages.value.length - 1]

    sending.value = true
    controller = new AbortController()
    try {
      const result = await chatStream(
        threadId.value,
        content,
        {
          onToken: (t) => (bot.content += t),
          onToolResult: (_name, text) => bot.tools!.push(text),
        },
        controller.signal,
      )
      // 逐段拼出来的文字可能包含模型调用工具之前说的话，以后端整理好的最终回复为准
      if (result.reply) bot.content = result.reply
      canUndo.value = true
      onPlanChanged(result.plan)
    } catch (error) {
      const err = error as Error
      if (err.name === 'AbortError') {
        bot.content ||= '（已停止）'
      } else {
        bot.error = true
        bot.content ||= err.message
        message.error(err.message)
      }
    } finally {
      bot.pending = false
      sending.value = false
      controller = null
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

  return { messages, sending, undoing, canUndo, load, send, stop, undo }
}

export type TripChat = ReturnType<typeof useTripChat>
