<script setup lang="ts">
import zhCN from 'ant-design-vue/es/locale/zh_CN' // antd 组件的中文文案
import dayjs from 'dayjs'
import 'dayjs/locale/zh-cn' // 日期选择器里的星期、月份文字

dayjs.locale('zh-cn')
</script>

<template>
  <!-- ConfigProvider：给所有 antd 组件设置全局配置（这里是中文语言包） -->
  <a-config-provider :locale="zhCN">
    <a-layout class="app-layout">
      <a-layout-header class="app-header">HelloAgents 智能旅行助手</a-layout-header>
      <a-layout-content class="app-content">
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
  font-size: 18px;
  font-weight: 600;
}

.app-content {
  max-width: 960px;
  width: 100%;
  margin: 0 auto;
  padding: 24px 16px;
}
</style>
