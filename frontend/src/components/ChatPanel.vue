<script setup lang="ts">
// P5：对话面板。只负责显示和输入，状态都在 useTripChat 里（由结果页创建后传进来）
import { nextTick, ref, watch } from 'vue'
import type { TripChat } from '@/composables/useTripChat'

const props = defineProps<{ chat: TripChat }>()

// 快捷指令：点击后填进输入框（不直接发送），用户可以把"第 1 天"改成别的再发
const QUICK_COMMANDS = ['第 1 天轻松一点', '换一家晚餐', '预算再省一点']

const input = ref('')
const listRef = ref<HTMLElement>()

function submit() {
  if (!input.value.trim()) return
  props.chat.send(input.value)
  input.value = ''
}

// Enter 发送，Shift + Enter 换行；输入法正在组字（isComposing）时按 Enter 是选词，不能发送
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault()
    submit()
  }
}

// 有新消息或新文字时滚动到底部。deep：气泡里的文字一点点变长也要触发
watch(
  () => props.chat.messages.value,
  async () => {
    await nextTick() // 等 DOM 更新完再滚动，否则拿到的是旧的高度
    listRef.value?.scrollTo({ top: listRef.value.scrollHeight })
  },
  { deep: true },
)
</script>

<template>
  <div class="chat-panel">
    <div class="chat-header">
      <span class="chat-title">💬 对话修改行程</span>
      <a-button
        size="small"
        :disabled="!chat.canUndo.value || chat.sending.value"
        :loading="chat.undoing.value"
        @click="chat.undo()"
      >
        ↩︎ 撤销
      </a-button>
    </div>

    <div ref="listRef" class="chat-list">
      <div v-if="!chat.messages.value.length" class="chat-empty">
        对行程哪里不满意，直接告诉我。<br />
        比如："第二天太累了，少安排一个景点"
      </div>

      <div v-for="(m, i) in chat.messages.value" :key="i" :class="['bubble-row', m.role]">
        <div :class="['bubble', { error: m.error }]">
          <!-- 工具执行结果：模型实际做了哪些修改 -->
          <div v-if="m.tools?.length" class="bubble-tools">
            <a-tag v-for="(t, j) in m.tools" :key="j" color="blue" class="tool-tag">🔧 {{ t }}</a-tag>
          </div>
          <span v-if="m.content">{{ m.content }}</span>
          <a-spin v-else-if="m.pending" size="small" />
        </div>
      </div>
    </div>

    <div class="chat-quick">
      <a-tag
        v-for="cmd in QUICK_COMMANDS"
        :key="cmd"
        class="quick-tag"
        @click="input = cmd"
      >
        {{ cmd }}
      </a-tag>
    </div>

    <div class="chat-input">
      <a-textarea
        v-model:value="input"
        :auto-size="{ minRows: 1, maxRows: 4 }"
        :maxlength="500"
        :disabled="chat.sending.value"
        placeholder="说说想怎么改，Enter 发送"
        @keydown="onKeydown"
      />
      <a-button v-if="chat.sending.value" danger @click="chat.stop()">停止</a-button>
      <a-button v-else type="primary" :disabled="!input.trim()" @click="submit">发送</a-button>
    </div>
  </div>
</template>

<style scoped>
.chat-panel {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0; /* 让中间的消息列表可以收缩并出现滚动条 */
}

.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding-bottom: 12px;
  border-bottom: 1px solid #f0f0f0;
}

.chat-title {
  font-weight: 600;
}

.chat-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 12px 0;
}

.chat-empty {
  color: rgba(0, 0, 0, 0.45);
  text-align: center;
  padding: 32px 8px;
  line-height: 1.8;
}

.bubble-row {
  display: flex;
  margin-bottom: 12px;
}

.bubble-row.user {
  justify-content: flex-end;
}

.bubble {
  max-width: 85%;
  padding: 8px 12px;
  border-radius: 12px;
  white-space: pre-wrap; /* 保留模型回复里的换行 */
  word-break: break-word;
  line-height: 1.6;
}

.user .bubble {
  background: #1677ff;
  color: #fff;
  border-bottom-right-radius: 4px;
}

.assistant .bubble {
  background: #f5f5f5;
  border-bottom-left-radius: 4px;
}

.bubble.error {
  background: #fff2f0;
  color: #cf1322;
}

.bubble-tools {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  margin-bottom: 6px;
}

.tool-tag {
  white-space: normal; /* 工具结果可能较长，允许换行 */
  margin-inline-end: 0;
}

.chat-quick {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 8px 0;
}

.quick-tag {
  cursor: pointer;
  margin-inline-end: 0;
}

.chat-input {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}
</style>
