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
    { path: '/result', name: 'result', component: Result }, // 结果页
  ],
})

// 全量注册 antd 组件，模板里可以直接用 <a-button> 等
createApp(App).use(router).use(Antd).mount('#app')
