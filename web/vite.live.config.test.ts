import { describe, expect, it } from 'vitest'

import { LIVE_API_PROXY_TARGET, validateLiveEnvironment } from './vite.live.guard'

const validEnvironment = {
  VITE_API_BASE_URL: '',
  VITE_API_PROXY_TARGET: LIVE_API_PROXY_TARGET,
  VITE_USE_MOCK: 'false',
}

describe('isolated Live Vite configuration', () => {
  it('accepts only the dedicated loopback API on port 8011', () => {
    expect(validateLiveEnvironment(validEnvironment)).toBe('http://127.0.0.1:8011')
  })

  it.each([
    undefined,
    '',
    'http://localhost:8011',
    'https://127.0.0.1:8011',
    'http://127.0.0.1:8000',
    'http://127.0.0.1:8011/',
    'http://192.168.1.10:8011',
  ])('rejects unsafe or ambiguous proxy target %s', (target) => {
    expect(() =>
      validateLiveEnvironment({
        ...validEnvironment,
        VITE_API_PROXY_TARGET: target,
      }),
    ).toThrow(/VITE_API_PROXY_TARGET=http:\/\/127\.0\.0\.1:8011/)
  })

  it('rejects Mock mode', () => {
    expect(() =>
      validateLiveEnvironment({
        ...validEnvironment,
        VITE_USE_MOCK: 'true',
      }),
    ).toThrow('VITE_USE_MOCK=false')
  })

  it('rejects a direct API base URL that would bypass the isolated proxy', () => {
    expect(() =>
      validateLiveEnvironment({
        ...validEnvironment,
        VITE_API_BASE_URL: 'http://127.0.0.1:8011',
      }),
    ).toThrow('empty VITE_API_BASE_URL')
  })
})
