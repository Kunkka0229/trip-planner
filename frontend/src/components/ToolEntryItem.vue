<script setup lang="ts">
// 一条工具结果的显示。P10 抽出来：委派步骤里的专家内部结果也用它显示（组件按文件名递归引用自己）
import type { ToolEntry } from '@/composables/useTripChat'

defineProps<{ entry: ToolEntry }>()
</script>

<template>
  <!-- P10：委派步骤。专家调用过工具时可以展开看内部步骤（默认收起）；没调用工具时只显示结论 -->
  <details v-if="entry.expert && entry.steps?.length" class="tool-box tool-group">
    <summary>
      <a-spin v-if="!entry.done" size="small" class="group-spin" />
      {{ entry.icon }} {{ entry.text }}
    </summary>
    <div class="group-steps">
      <ToolEntryItem v-for="(s, i) in entry.steps" :key="i" :entry="s" />
    </div>
  </details>
  <!-- 查看的候选、P8 搜索到的新候选：点开看名字和价格（<details> 是浏览器自带的折叠，不用写状态） -->
  <details v-else-if="entry.places" class="tool-box">
    <summary>{{ entry.icon }} {{ entry.text }}</summary>
    <ul>
      <li v-for="p in entry.places" :key="p.name">
        <span>{{ p.name }}<span class="place-district">{{ p.district }}</span></span>
        <span v-if="p.price != null" class="place-price">¥{{ p.price }}</span>
      </li>
    </ul>
  </details>
  <!-- P9：查到的攻略出处 -->
  <details v-else-if="entry.guides" class="tool-box">
    <summary>{{ entry.icon }} {{ entry.text }}</summary>
    <ul>
      <li v-for="g in entry.guides" :key="g.n">
        <span>[{{ g.n }}] 《{{ g.title }}》{{ g.section }}</span>
        <span class="place-district">{{ g.updatedAt }}</span>
      </li>
    </ul>
  </details>
  <!-- P10：太长的原始结果（12306 余票列表等） -->
  <details v-else-if="entry.detail" class="tool-box">
    <summary>{{ entry.icon }} {{ entry.text }}</summary>
    <pre class="tool-detail">{{ entry.detail }}</pre>
  </details>
  <a-tag v-else color="blue" class="tool-tag">
    <a-spin v-if="entry.expert && !entry.done" size="small" class="group-spin" />
    {{ entry.icon }} {{ entry.text }}
  </a-tag>
</template>

<style scoped>
.tool-tag {
  white-space: normal; /* 工具结果可能较长，允许换行 */
  margin-inline-end: 0;
}

.tool-box {
  width: 100%;
  padding: 4px 8px;
  border: 1px solid #91caff;
  border-radius: 4px;
  background: #e6f4ff;
  color: #0958d9;
  font-size: 12px;
}

.tool-box summary {
  cursor: pointer;
}

.tool-box ul {
  margin: 4px 0 0;
  padding: 0;
  list-style: none;
}

.tool-box li {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  line-height: 1.8;
}

.place-district {
  margin-left: 6px;
  color: rgba(0, 0, 0, 0.45);
}

.place-price {
  flex-shrink: 0;
}

.tool-detail {
  max-height: 200px;
  margin: 4px 0 0;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 11px;
  color: rgba(0, 0, 0, 0.65);
}

/* 委派步骤：外框用紫色和普通工具结果区分，展开后内部步骤缩进显示 */
.tool-group {
  border-color: #d3adf7;
  background: #f9f0ff;
  color: #531dab;
}

.group-steps {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  margin-top: 6px;
  padding-left: 10px;
  border-left: 2px solid #d3adf7;
}

.group-spin {
  margin-right: 4px;
}
</style>
