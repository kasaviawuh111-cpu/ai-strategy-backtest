import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

import { validateLiveEnvironment } from './vite.live.guard'

export default defineConfig(({ mode }) => {
  const fileEnvironment = loadEnv(mode, process.cwd(), '')
  const proxyTarget = validateLiveEnvironment({
    VITE_API_PROXY_TARGET:
      process.env.VITE_API_PROXY_TARGET ?? fileEnvironment.VITE_API_PROXY_TARGET,
    VITE_USE_MOCK: process.env.VITE_USE_MOCK ?? fileEnvironment.VITE_USE_MOCK,
    VITE_API_BASE_URL:
      process.env.VITE_API_BASE_URL ?? fileEnvironment.VITE_API_BASE_URL ?? '',
  })

  return {
    plugins: [react()],
    server: {
      host: '127.0.0.1',
      port: 5184,
      strictPort: true,
      proxy: {
        '/api': { target: proxyTarget },
        '/health': { target: proxyTarget },
      },
    },
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.ts'],
      css: true,
    },
  }
})
