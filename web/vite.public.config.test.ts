import { describe, expect, it } from 'vitest'

import { validatePublicLiveEnvironment } from './vite.public.guard'

const validEnvironment = {
  VITE_API_BASE_URL: 'https://api.example.cn',
  VITE_USE_MOCK: 'false',
}

describe('public Live Vite configuration', () => {
  it('accepts a public HTTPS API origin in non-Mock mode', () => {
    expect(validatePublicLiveEnvironment(validEnvironment)).toBe('https://api.example.cn')
  })

  it('rejects Mock mode and a missing API origin', () => {
    expect(() => validatePublicLiveEnvironment({
      ...validEnvironment,
      VITE_USE_MOCK: 'true',
    })).toThrow('VITE_USE_MOCK=false')
    expect(() => validatePublicLiveEnvironment({
      ...validEnvironment,
      VITE_API_BASE_URL: '',
    })).toThrow('non-empty VITE_API_BASE_URL')
  })

  it.each([
    'http://api.example.cn',
    'http://127.0.0.1:8011',
    'https://localhost:8011',
    'https://192.168.1.20',
    'https://api.example.cn/v1',
    'https://user:pass@api.example.cn',
    'https://api.example.cn?mode=live',
  ])('rejects an unsafe or ambiguous API base URL %s', (baseUrl) => {
    expect(() => validatePublicLiveEnvironment({
      ...validEnvironment,
      VITE_API_BASE_URL: baseUrl,
    })).toThrow('public HTTPS origin')
  })
})
