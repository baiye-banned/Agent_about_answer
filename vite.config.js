import { defineConfig } from 'vite'
import Vue from '@vitejs/plugin-vue'
import AutoImport from 'unplugin-auto-import/vite'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [
    Vue(),
    AutoImport({
      resolvers: [ElementPlusResolver({ importStyle: 'css', directives: true })],
      imports: ['vue', 'vue-router', 'pinia'],
      dts: false,
    }),
    Components({
      resolvers: [ElementPlusResolver({ importStyle: 'css', directives: true })],
      dts: false,
    }),
  ],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    rollupOptions: {
      input: 'index.html',
    },
  },
  server: {
    port: 5173,
    // 端口被占用时直接失败，不要顺延到 5174：README 把 http://localhost:5173 当作既定地址，
    // 顺延时 vite 只打一行 info 就继续启动，那个地址会静默指向占用该端口的别的服务。
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8002',
        changeOrigin: true,
      },
      // 头像读取面带鉴权之后（issue #186），`/uploads/...` 不再由静态挂载匿名直出，而是前端
      // 带着 token 去取。开发时这条要有代理才落到后端，否则请求打在 5173 上拿到的是前端页面；
      // 与部署用 README 里 nginx 的 `location /uploads/` 是同一件事。
      '/uploads': {
        target: 'http://localhost:8002',
        changeOrigin: true,
      },
    },
  },
})
