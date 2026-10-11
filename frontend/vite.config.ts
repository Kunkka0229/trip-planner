import { fileURLToPath, URL } from 'node:url'
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      // 让 import '@/types' 指向 src/types，避免写一长串 ../../
      // （还需要在 tsconfig.app.json 里配 paths，TS 才认识这个别名）
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 开发代理：浏览器请求 http://localhost:5173/api/xxx
    // → Vite 开发服务器转发到 http://127.0.0.1:7001/api/xxx
    // 对浏览器来说请求的是同源地址，所以开发阶段根本不会触发 CORS
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:7001',
        changeOrigin: true, // 把请求头里的 Host 改成目标地址
      },
    },
  },
})
