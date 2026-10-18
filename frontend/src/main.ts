import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css' // antd 的基础样式重置
import App from './App.vue'
import Home from './views/Home.vue'
import Result from './views/Result.vue'

const router = createRouter({
  history: createWebHistory(), // HTML5 history 模式，URL 里没有 #
  routes: [
    { path: '/', name: 'home', component: Home }, // 表单页
    // P5：结果页带上 thread_id（:threadId? 的 ? 表示可选）
    //   有 thread_id：进入页面时从后端恢复行程和对话，刷新、换设备打开都不会丢
    //   没有 thread_id：没配置大模型时的 mock 数据，没有会话，退回读 sessionStorage
    // meta.wide：结果页右侧有对话面板，需要更宽的内容区（App.vue 里读取）
    { path: '/result/:threadId?', name: 'result', component: Result, meta: { wide: true } },
  ],
})

// 全量注册 antd 组件，模板里可以直接用 <a-button> 等
createApp(App).use(router).use(Antd).mount('#app')
