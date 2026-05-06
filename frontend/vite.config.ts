import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// ScriptLens 前端 dev server
//   - port=5174（避开 ScholarMind dev 的 5173）
//   - /api 反代 ScriptLens 后端 8005（docker-compose.dev.yml 暴露端口）
//   - alias @ → /src/（跟 ScholarMind 一致，方便组件 copy 时不改 import）
export default defineConfig(() => {
  return {
    server: {
      port: 5174,
      host: '0.0.0.0',
      strictPort: false,
      proxy: {
        '/api': {
          target: 'http://127.0.0.1:8005',
          changeOrigin: true,
        },
      },
    },
    plugins: [react()],
    resolve: {
      alias: [
        {
          find: /^@\//,
          replacement: '/src/',
        },
      ],
    },
  }
})
