<script setup lang="ts">
import zhCN from 'ant-design-vue/es/locale/zh_CN' // antd 组件的中文文案
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn' // 日期选择器里的星期、月份文字
import { useRoute } from 'vue-router'

dayjs.locale('zh-cn')

const route = useRoute()
</script>

<template>
  <!-- ConfigProvider：给所有 antd 组件设置全局配置（这里是中文语言包） -->
  <a-config-provider :locale="zhCN">
    <a-layout class="app-layout">
      <a-layout-header class="app-header">
        <router-link to="/" class="app-title">HelloAgents 智能旅行助手</router-link>
        <!-- P7：个人中心入口，查看和删除 AI 记住的偏好 -->
        <router-link to="/memories" class="app-nav">🧠 AI 记住了什么</router-link>
      </a-layout-header>
      <!-- P5：结果页（meta.wide）多了对话面板，内容区放宽 -->
      <a-layout-content :class="['app-content', { 'app-content-wide': route.meta.wide }]">
        <router-view />
      </a-layout-content>
    </a-layout>
  </a-config-provider>
</template>

<style>
.app-layout {
  min-height: 100vh;
  background: #f6f8fb;
}

/* 写成 .app-layout .app-header 提高优先级，才能覆盖 antd 默认的深色 Header */
.app-layout .app-header {
  background: #fff;
  border-bottom: 1px solid #e2e8f0;
  color: #0f172a;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.app-title {
  color: #0f172a;
  font-size: 18px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.app-nav {
  font-size: 14px;
  white-space: nowrap;
}

.app-content {
  max-width: 960px;
  width: 100%;
  margin: 0 auto;
  padding: 24px 16px;
}

.app-content-wide {
  max-width: 1280px;
}
</style>
