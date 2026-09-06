import { afterEach, describe, expect, it, vi } from 'vitest'
import { instrumentApi } from './client'

afterEach(() => { vi.unstubAllGlobals() })

describe('B28 real instrument-search client contract', () => {
  it('requests the generic endpoint and preserves the provider identity and partial-list flag', async () => {
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      query: '中国', source: 'eastmoney_security_search', retrieved_at: '2026-09-06T08:00:00Z',
      items: [{ name: '中国平安', symbol: '601318.SH', exchange: 'SH' }], has_more: true,
    }), { status: 200 }))
    vi.stubGlobal('fetch', fetch)
    const result = await instrumentApi.search('中国', new AbortController().signal)
    expect(fetch.mock.calls[0]?.[0]).toBe('/api/v1/market/instruments?query=%E4%B8%AD%E5%9B%BD&limit=8')
    expect(result).toEqual({ items: [{ name: '中国平安', symbol: '601318.SH', exchange: 'SSE', market: 'CN_A' }], hasMore: true })
  })

  it('rejects an invalid identity instead of presenting its label as a confirmed stock', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      source: 'eastmoney_security_search', has_more: false,
      items: [{ name: '错配名称', symbol: '601318.SH', exchange: 'SZ' }],
    }), { status: 200 })))
    await expect(instrumentApi.search('错配名称', new AbortController().signal))
      .rejects.toMatchObject({ problem: { code: 'instrument_search_invalid' } })
  })

  it('accepts the packaged directory without weakening identity checks or losing its stale flag', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({
      source: 'eastmoney_instrument_directory', has_more: true, cache_status: 'stale_cache',
      items: [{ name: '艾融软件', symbol: '920799.BJ', exchange: 'BJ' }],
    }), { status: 200 })))
    await expect(instrumentApi.search('艾融', new AbortController().signal)).resolves.toEqual({
      items: [{ name: '艾融软件', symbol: '920799.BJ', market: 'CN_A', exchange: 'BSE' }],
      hasMore: true, isStaleCache: true,
    })
  })
})
