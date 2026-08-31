import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

import { validatePublicLiveEnvironment } from './vite.public.guard'

export default defineConfig(({ mode }) => {
  const fileEnvironment = loadEnv(mode, process.cwd(), '')
  validatePublicLiveEnvironment({
    VITE_USE_MOCK: process.env.VITE_USE_MOCK ?? fileEnvironment.VITE_USE_MOCK,
    VITE_API_BASE_URL:
      process.env.VITE_API_BASE_URL ?? fileEnvironment.VITE_API_BASE_URL,
  })

  return {
    plugins: [react()],
    test: {
      environment: 'jsdom',
      globals: true,
      setupFiles: ['./src/test/setup.ts'],
      css: true,
    },
  }
})
