/// <reference types="vitest/config" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/test/setup.ts'],
    // jsdom 在 WSL / CI 上的初始化與互動事件明顯偏慢（實測 environment
    // setup 就可能要 20s+），預設 5s 會讓 userEvent 互動測試假性 timeout。
    // 拉高門檻是為了避免假紅燈，不是為了容忍真正的慢測試。
    testTimeout: 20_000,
    hookTimeout: 20_000,
    // 測試不得打真實網路——後端 API 一律用 mock（見 src/test/setup.ts）
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/main.tsx', 'src/test/**', '**/*.test.{ts,tsx}'],
      reporter: ['text-summary', 'text'],
    },
  },
})
