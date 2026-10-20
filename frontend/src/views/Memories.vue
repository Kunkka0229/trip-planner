<script setup lang="ts">
// P7："AI 记住了什么"页面：查看、删除长期记忆（《个人信息保护法》要求用户能查阅、删除自己的个人信息）
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { message } from 'ant-design-vue'
import dayjs from 'dayjs'
import { clearMemories, deleteMemory, getMemories } from '@/services/api'
import type { MemoryItem } from '@/types'
import { MEMORY_CATEGORIES, normalizeCategory } from '@/utils/memory'

const router = useRouter()

const memories = ref<MemoryItem[]>([])
const loading = ref(false)
const deletingKey = ref<string | null>(null) // 正在删除哪一条：只让那一条的按钮转圈
const clearing = ref(false)

// 按分类分组，并按 MEMORY_CATEGORIES 的顺序排列；空的分类不显示
const groups = computed(() =>
  MEMORY_CATEGORIES.map((c) => ({
    ...c,
    items: memories.value.filter((m) => normalizeCategory(m.category) === c.key),
  })).filter((g) => g.items.length > 0),
)

async function load() {
  loading.value = true
  try {
    memories.value = await getMemories()
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    loading.value = false
  }
}

async function remove(key: string) {
  deletingKey.value = key
  try {
    await deleteMemory(key)
    // 不重新请求整个列表：本地去掉这一条就行，界面更快
    memories.value = memories.value.filter((m) => m.key !== key)
    message.success('已删除')
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    deletingKey.value = null
  }
}

async function clearAll() {
  clearing.value = true
  try {
    const count = await clearMemories()
    memories.value = []
    message.success(`已清空 ${count} 条记忆`)
  } catch (e) {
    message.error((e as Error).message)
  } finally {
    clearing.value = false
  }
}

onMounted(load)
</script>

<template>
  <a-card title="🧠 AI 记住了什么">
    <template #extra>
      <a-button @click="router.push({ name: 'home' })">去规划行程</a-button>
    </template>

    <p class="intro">
      规划和修改行程时，AI 会记下你的长期偏好（比如同行人员、饮食禁忌），下次规划时自动参考。
      你可以随时删除任何一条。
    </p>

    <a-spin :spinning="loading">
      <a-empty v-if="!loading && !memories.length" description="还没有记住任何偏好" />

      <div v-for="group in groups" :key="group.key" class="group">
        <h3 class="group-title">{{ group.icon }} {{ group.label }}</h3>
        <a-list :data-source="group.items" size="small" bordered>
          <template #renderItem="{ item }">
            <a-list-item>
              <a-list-item-meta :title="item.text" :description="`更新于 ${dayjs(item.updated_at).format('YYYY-MM-DD HH:mm')}`" />
              <template #actions>
                <a-popconfirm title="删除这条记忆？" ok-text="删除" cancel-text="取消" @confirm="remove(item.key)">
                  <a-button size="small" danger :loading="deletingKey === item.key">删除</a-button>
                </a-popconfirm>
              </template>
            </a-list-item>
          </template>
        </a-list>
      </div>

      <!-- 清空全部：二次确认，不可恢复 -->
      <div v-if="memories.length" class="footer">
        <a-popconfirm
          title="清空后 AI 将不再记得你的任何偏好，且无法恢复。确定清空吗？"
          ok-text="清空"
          cancel-text="取消"
          :ok-button-props="{ danger: true }"
          @confirm="clearAll"
        >
          <a-button danger :loading="clearing">清空全部记忆</a-button>
        </a-popconfirm>
      </div>
    </a-spin>
  </a-card>
</template>

<style scoped>
.intro {
  color: rgba(0, 0, 0, 0.45);
  margin-bottom: 16px;
}

.group {
  margin-bottom: 20px;
}

.group-title {
  font-size: 15px;
  margin-bottom: 8px;
}

.footer {
  display: flex;
  justify-content: flex-end;
  padding-top: 8px;
  border-top: 1px dashed #f0f0f0;
}
</style>
