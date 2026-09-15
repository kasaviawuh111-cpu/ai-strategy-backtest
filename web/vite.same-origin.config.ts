import react from '@vitejs/plugin-react'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

import { validateSameOriginEnvironment } from './vite.same-origin.guard'

export default defineConfig(({ mode }) => {
  const fileEnvironment = loadEnv(mode, process.cwd(), '')
  const environment = {
    VITE_USE_MOCK: process.env.VITE_USE_MOCK ?? fileEnvironment.VITE_USE_MOCK,
    VITE_API_BASE_URL:
      process.env.VITE_API_BASE_URL ?? fileEnvironment.VITE_API_BASE_URL,
  }
  validateSameOriginEnvironment(environment)

  return {
    base: '/',
    define: {
      'import.meta.env.VITE_USE_MOCK': JSON.stringify('false'),
      'import.meta.env.VITE_API_BASE_URL': JSON.stringify(''),
      // Live releases use the current calendar, never a replay fixture cutoff.
      'import.meta.env.VITE_DATA_AS_OF_DATE': JSON.stringify(''),
    },
    plugins: [react()],
  }
})
