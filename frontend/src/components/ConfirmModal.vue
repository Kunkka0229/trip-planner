<script setup lang="ts">
// P6：人工确认框。后端的图暂停在 interrupt() 处，这里显示问题，用户的选择通过 /resume 让图继续
//   budget：行程超预算，接受 / 降档重新规划
//   approval：修改 Agent 要预订酒店，批准 / 拒绝（拒绝可以附理由，会告诉模型）
import { computed, ref, watch } from 'vue'
import type { TripChat } from '@/composables/useTripChat'

const props = defineProps<{ chat: TripChat }>()

const question = computed(() => props.chat.current.value)
const busy = computed(() => props.chat.answering.value !== null)
// 提交后图在继续执行（降档会重新生成行程，可能要几十秒），把助手气泡里的阶段文字也显示在这里
const progressText = computed(() => {
  const last = props.chat.messages.value.at(-1)
  return busy.value && last?.pending ? last.status || '正在处理...' : ''
})

const rejectReason = ref('')
// 换了一个问题时清空上一次填的理由
watch(
  () => question.value?.id,
  () => (rejectReason.value = ''),
)

// 处理中不允许关闭：关掉后用户看不到进度，以为卡住了
function close() {
  if (!busy.value) props.chat.confirmOpen.value = false
}
</script>

<template>
  <a-modal
    :open="chat.confirmOpen.value && !!question"
    :title="question?.kind === 'budget' ? '💰 预算确认' : '🏨 预订确认'"
    :mask-closable="false"
    :closable="!busy"
    :footer="null"
    @cancel="close"
  >
    <template v-if="question">
      <p class="question">{{ question.question }}</p>

      <!-- 超预算：两个按钮的文字来自后端的 options -->
      <template v-if="question.kind === 'budget'">
        <p class="hint">行程已经显示在背后，可以先关掉这个窗口看看，再从对话面板回来处理。</p>
        <div class="actions">
          <a-button
            :loading="chat.answering.value === 'accept'"
            :disabled="busy"
            @click="chat.answer('accept')"
          >
            {{ question.options.accept }}
          </a-button>
          <a-button
            type="primary"
            :loading="chat.answering.value === 'downgrade'"
            :disabled="busy"
            @click="chat.answer('downgrade')"
          >
            {{ question.options.downgrade }}
          </a-button>
        </div>
      </template>

      <!-- 预订审批：question 已经是后端写好的人话（describe_booking） -->
      <template v-else>
        <a-textarea
          v-model:value="rejectReason"
          :rows="2"
          :maxlength="200"
          :disabled="busy"
          placeholder="如果拒绝，可以说说原因（选填），比如：想换一家离景点近的"
        />
        <div class="actions">
          <a-button
            danger
            :loading="chat.answering.value === 'reject'"
            :disabled="busy"
            @click="chat.answer('reject', rejectReason.trim())"
          >
            拒绝
          </a-button>
          <a-button
            type="primary"
            :loading="chat.answering.value === 'approve'"
            :disabled="busy"
            @click="chat.answer('approve')"
          >
            批准
          </a-button>
        </div>
      </template>

      <p v-if="progressText" class="progress">⏳ {{ progressText }}</p>
    </template>
  </a-modal>
</template>

<style scoped>
.question {
  font-size: 15px;
  line-height: 1.7;
}

.hint {
  color: rgba(0, 0, 0, 0.45);
  font-size: 13px;
}

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}

.progress {
  margin: 12px 0 0;
  color: rgba(0, 0, 0, 0.45);
}
</style>
