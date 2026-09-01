import { describe, expect, it } from 'vitest'

import { validateSameOriginEnvironment } from './vite.same-origin.guard'

const validEnvironment = {
  VITE_API_BASE_URL: '',
  VITE_USE_MOCK: 'false',
}

describe('same-origin Live Vite configuration', () => {
  it('accepts non-Mock mode with an explicitly empty API base URL', () => {
    expect(() => validateSameOriginEnvironment(validEnvironment)).not.toThrow()
  })

  it.each([undefined, '', 'true', 'TRUE'])('rejects Mock or ambiguous mode %s', (useMock) => {
    expect(() => validateSameOriginEnvironment({
      ...validEnvironment,
      VITE_USE_MOCK: useMock,
    })).toThrow('VITE_USE_MOCK=false')
  })

  it.each([
    undefined,
    ' ',
    '/',
    '/api',
    'https://ashare-backtest-api-305722-11-1330091763.sh.run.tcloudbase.com',
    'https://api.example.cn',
  ])('rejects a non-empty or implicit API base URL %s', (apiBaseUrl) => {
    expect(() => validateSameOriginEnvironment({
      ...validEnvironment,
      VITE_API_BASE_URL: apiBaseUrl,
    })).toThrow('VITE_API_BASE_URL to be explicitly empty')
  })
})
