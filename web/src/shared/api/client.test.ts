import type {
  BacktestReviewResponse,
  CapabilitiesResponse,
  CompileRequest,
  StrategySpec,
} from './types'
import type { LiveDraftResponse } from './contract'
import type { DialogueProgressObserver, PreviewPollRecovery } from './client'

const clarifiedRequest: CompileRequest = {
  utterance: '东方财富 MACD 金叉买入，死叉卖出',
  instrument: {
    name: '东方财富',
    symbol: '300059.SZ',
    market: 'CN_A',
    exchange: 'SZSE',
  },
}

const capabilities = (overrides: Partial<CapabilitiesResponse> = {}): CapabilitiesResponse => ({
  markets: ['CN_A'],
  input_modes: ['natural_language_zh'],
  strategy_scopes: ['single_instrument', 'long_only'],
  indicators: [],
  events: [],
  execution_policies: ['next_tradable_session_open'],
  event_catalog_status: 'published',
  event_backtest_available: false,
  event_preparation_available: false,
  event_availability_scope: 'unavailable',
  backtest_execution_available: true,
  limits: {
    max_body_bytes: 16_384,
    max_utterance_characters: 2000,
    max_instrument_context_characters: 32,
  },
  ...overrides,
})

const eventStrategy: StrategySpec = {
  schema_version: 'strategy.v1',
  catalog: { catalog_id: 'cn_a.signals', release_version: '2026.08.29' },
  instrument: { market: 'CN_A', symbol: '300059.SZ', position_mode: 'long_only' },
  entry: {
    type: 'event_condition',
    event_code: 'event.financial_results.annual_report',
    definition_version: '1.0.0',
    trigger: 'published',
    attributes: {},
  },
  exit: {
    op: 'first_of',
    children: [{
      type: 'indicator_condition',
      indicator_id: 'technical.macd',
      definition_version: '1.0.0',
      params: { fast: 12, slow: 26, signal: 9 },
      timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed',
      trigger: 'death_cross',
      value: null,
    }],
  },
  execution: {
    timezone: 'Asia/Shanghai',
    entry_policy: 'next_tradable_session_open',
    exit_policy: 'next_tradable_session_open',
    data_capability: 'daily_ohlcv_events',
    execution_resolution: '1d',
    evaluation_frequency: 'event_available_plus_1d_close',
    position_policy: 'single_position_no_pyramiding',
    t_plus_one: true,
  },
  backtest: { start: '2021-08-06', end: '2026-08-06', initial_cash_cny: 1_000_000 },
}

const documentTextStrategy: StrategySpec = {
  ...eventStrategy,
  entry: {
    type: 'event_condition',
    event_code: 'event.financial_results.annual_report',
    definition_version: '1.0.0',
    trigger: 'published',
    attributes: {},
    document_text: {
      metric_id: 'document.literal_mention_count',
      metric_version: '1.0.0',
      term: 'AI',
      normalization: 'nfkc',
      match_mode: 'ascii_token',
      case_sensitive: false,
      comparator: 'gt',
      value: 5,
    },
  },
}

const readyResponse = (strategy: StrategySpec = eventStrategy): LiveDraftResponse => ({
  draft_id: '8c91eb84-ab49-4b0c-890a-682e9cc6fe21',
  revision: 1,
  status: 'ready',
  strategy,
  strategy_hash: `sha256:${'a'.repeat(64)}`,
  clarification: null,
  diagnostic_code: null,
  provenance: [],
  candidate_provenance: null,
  candidate_grounding: null,
  candidate_alternatives: [],
  candidate_rejections: [],
  created_at: '2026-08-29T00:00:00Z',
})

describe('live strategy client', () => {
  it.each(['unavailable', 'wrong-stock', 'invalid-field', 'unknown-unit', 'valid']) (
    'filters metric search evidence before offering it: %s', async state => {
      vi.stubEnv('VITE_USE_MOCK', 'false')
      const payload = {
        instrument_id: state === 'wrong-stock' ? '600519.SH' : '002558.SZ',
        instrument_verified: true, status: state === 'unavailable' ? 'unavailable' : 'discovered',
        candidate_table_indices: [0], tables: [{ table_index: 0, instrument_verified: true,
          issues: [], fields: [{ return_name: '换手率', display_name: '换手率',
            unit: state === 'unknown-unit' ? null : '%', values: ['1', '2'],
            issues: state === 'invalid-field' ? ['field_length_mismatch'] : [] }] }],
      }
      vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(payload), {status: 200})))
      const { metricApi } = await import('./client')
      const result = await metricApi.discover({instrument_id:'002558.SZ', metric_query:'换手率',
        start:'2025-09-08', end:'2026-09-08'})
      expect(result).toHaveLength(state === 'valid' ? 1 : 0)
    },
  )
  it('prepares the complete run payload without creating a run or a draft revision', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const { fromLiveDraftResponse, toLiveBacktestBody } = await import('./contract')
    const compiled = fromLiveDraftResponse(readyResponse(), clarifiedRequest)
    if (compiled.status !== 'compiled') throw new Error('expected compiled StrategySpec')
    const timeout = vi.spyOn(window, 'setTimeout')
    const fetcher = vi.fn<(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>>(async () =>
      new Response(JSON.stringify({ ready: true }), { status: 200 }))
    vi.stubGlobal('fetch', fetcher)
    const { backtestApi } = await import('./client')
    const controller = new AbortController()
    await expect(backtestApi.prepare(compiled.draft, controller.signal)).resolves.toEqual({ ready: true })
    expect(fetcher).toHaveBeenCalledTimes(1)
    expect(fetcher.mock.calls[0]?.[0]).toBe('/api/v1/backtest-runs/prepare')
    const init = fetcher.mock.calls[0]?.[1]
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual(toLiveBacktestBody(compiled.draft))
    expect(init?.signal).toBeInstanceOf(AbortSignal)
    expect(timeout).toHaveBeenCalledWith(expect.any(Function), 300_000)
  })

  it('preserves concrete preparation errors returned through preview polling', async () => {
    vi.useFakeTimers()
    vi.stubEnv('VITE_USE_MOCK', 'false')
    vi.stubEnv('VITE_PRIVATE_PREVIEW', 'true')
    const { fromLiveDraftResponse } = await import('./contract')
    const compiled = fromLiveDraftResponse(readyResponse(), clarifiedRequest)
    if (compiled.status !== 'compiled') throw new Error('expected compiled StrategySpec')
    const location = '/api/v1/preview-requests/12345678-1234-1234-1234-123456789abc'
    const detail = '回测起点早于股票上市日 2026-07-27，请调整回测区间。'
    const fetcher = vi.fn(async (input: RequestInfo | URL) => String(input) === location
      ? new Response(JSON.stringify({ title: '区间不可用', detail,
        code: 'skill_history_before_listing', status: 422 }), { status: 422,
        headers: { 'Content-Type': 'application/problem+json' } })
      : new Response('{}', { status: 202, headers: { Location: location, 'X-Preview-Pending': '1' } }))
    vi.stubGlobal('fetch', fetcher)
    const { backtestApi } = await import('./client')
    const result = expect(backtestApi.prepare(compiled.draft)).rejects.toMatchObject({
      problem: expect.objectContaining({ code: 'skill_history_before_listing', detail }),
    })
    await vi.advanceTimersByTimeAsync(1_000)
    await result
    expect(fetcher.mock.calls.map(([path]) => path)).toEqual(['/api/v1/backtest-runs/prepare', location])
  })

  it('polls preview model requests without resubmitting the original POST', async () => {
    vi.stubEnv('VITE_PRIVATE_PREVIEW', 'true')
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const location = '/api/v1/preview-requests/12345678-1234-1234-1234-123456789abc'
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/v1/capabilities') {
        return new Response(JSON.stringify(capabilities()))
      }
      if (String(input) === '/api/v1/strategy-drafts') {
        expect(new Headers(init?.headers).get('Prefer')).toBe('respond-async')
        expect(new Headers(init?.headers).get('X-Preview-Client-ID')).toMatch(/^[0-9a-f-]{36}$/)
        return new Response('{}', { status: 202,
          headers: { Location: location, 'X-Preview-Pending': '1' } })
      }
      expect(String(input)).toBe(location)
      expect(init?.redirect).toBe('error')
      return new Response(JSON.stringify(readyResponse()), { status: 201 })
    })
    vi.stubGlobal('fetch', fetcher)
    const { strategyApi } = await import('./client')
    await strategyApi.compile(clarifiedRequest)
    expect(fetcher.mock.calls.filter(([url]) => String(url) === '/api/v1/strategy-drafts')).toHaveLength(1)
    expect(fetcher.mock.calls.filter(([url]) => String(url) === location)).toHaveLength(1)
  })

  it('retries an idempotent dialogue POST after a transient socket failure', async () => {
    vi.useFakeTimers()
    vi.stubEnv('VITE_USE_MOCK', 'false')
    let draftAttempts = 0
    const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') return new Response(JSON.stringify(capabilities()))
      if (path.startsWith('/api/v1/dialogue-progress/')) {
        return new Response(JSON.stringify({ finished: true, events: [] }))
      }
      if (path === '/api/v1/strategy-drafts') {
        draftAttempts += 1
        expect(new Headers(init?.headers).get('Idempotency-Key')).toMatch(/^draft:/)
        if (draftAttempts === 1) throw new TypeError('socket closed during reload')
        return new Response(JSON.stringify(readyResponse()), { status: 201 })
      }
      throw new Error(`unexpected fetch: ${path}`)
    })
    vi.stubGlobal('fetch', fetcher)

    const { strategyApi } = await import('./client')
    const compile = strategyApi.compile({ ...clarifiedRequest, dialogueProgress: {
      onProgress: vi.fn(),
    } })
    await vi.advanceTimersByTimeAsync(250)
    await expect(compile).resolves.toMatchObject({ status: 'compiled' })
    expect(draftAttempts).toBe(2)
  })

  it('rejects an external preview poll address without following it', async () => {
    vi.stubEnv('VITE_PRIVATE_PREVIEW', 'true')
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const fetcher = vi.fn(async (input: RequestInfo | URL) => (
      String(input) === '/api/v1/capabilities'
        ? new Response(JSON.stringify(capabilities()))
        : new Response('{}', { status: 202, headers: {
          Location: 'https://example.invalid/steal', 'X-Preview-Pending': '1',
        } })
    ))
    vi.stubGlobal('fetch', fetcher)
    const { strategyApi } = await import('./client')
    await expect(strategyApi.compile(clarifiedRequest)).rejects.toMatchObject({
      problem: expect.objectContaining({ code: 'preview_invalid_location' }),
    })
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  describe('accepted preview request recovery', () => {
    const location = '/api/v1/preview-requests/12345678-1234-1234-1234-123456789abc'
    const ready = () => new Response(JSON.stringify(readyResponse()), { status: 201 })
    const begin = async (
      poll: (attempt: number, signal: AbortSignal) => Promise<Response>,
      recover = true,
    ) => {
      vi.useFakeTimers()
      vi.stubEnv('VITE_PRIVATE_PREVIEW', 'true')
      vi.stubEnv('VITE_USE_MOCK', 'false')
      const controller = new AbortController()
      const onRecovery = vi.fn<(state: PreviewPollRecovery | null) => void>()
      const onProgress = vi.fn<DialogueProgressObserver['onProgress']>()
      let queries = 0
      const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input)
        if (path === '/api/v1/capabilities') return new Response(JSON.stringify(capabilities()))
        if (path.startsWith('/api/v1/dialogue-progress/')) return new Response(JSON.stringify({
          finished: false, events: [{ stage: 'model', message: '模型正在分析', elapsed_ms: 1_000,
            reasoning: '真实返回的说明，不应因连接恢复而丢失。' }],
        }))
        if (path === '/api/v1/strategy-drafts') return new Response('{}', {
          status: 202, headers: { Location: location, 'X-Preview-Pending': '1' },
        })
        expect(path).toBe(location)
        expect(init?.method).toBeUndefined()
        expect(init?.redirect).toBe('error')
        return poll(++queries, init!.signal as AbortSignal)
      })
      vi.stubGlobal('fetch', fetcher)
      const { strategyApi } = await import('./client')
      const result = strategyApi.compile({ ...clarifiedRequest, dialogueProgress: {
        signal: controller.signal, onProgress, ...(recover ? { onRecovery } : {}),
      } })
      const expectOnePost = () => expect(fetcher.mock.calls.filter(([, init]) =>
        init?.method === 'POST')).toHaveLength(1)
      return { result, onRecovery, onProgress, controller, queries: () => queries, expectOnePost }
    }
    afterEach(() => { vi.useRealTimers() })

    it('recovers a single failed GET without requiring AbortSignal.any or timeout', async () => {
      vi.spyOn(AbortSignal, 'any').mockImplementation(() => { throw new Error('unsupported') })
      vi.spyOn(AbortSignal, 'timeout').mockImplementation(() => { throw new Error('unsupported') })
      const state = await begin(async attempt => {
        if (attempt === 1) throw new TypeError('Failed to fetch')
        return ready()
      })
      const result = expect(state.result).resolves.toMatchObject({ status: 'compiled' })
      await vi.advanceTimersByTimeAsync(1_000)
      expect(state.onRecovery).toHaveBeenLastCalledWith(expect.objectContaining({
        status: 'retrying', attempt: 1, message: expect.stringContaining('结果查询连接暂时中断'),
      }))
      await vi.advanceTimersByTimeAsync(1_000)
      await result
      expect(state.queries()).toBe(2)
      state.expectOnePost()
      expect(state.onRecovery).toHaveBeenLastCalledWith(null)
    })

    it.each(['manual', 'online'])('keeps the original result pending until %s recovery', async mode => {
      const state = await begin(async attempt => {
        if (attempt <= 4) throw new TypeError('offline')
        return ready()
      })
      let finished = false
      const result = state.result.then(value => { finished = true; return value })
      await vi.advanceTimersByTimeAsync(8_000)
      expect(finished).toBe(false)
      expect(state.queries()).toBe(4)
      const paused = state.onRecovery.mock.calls.at(-1)?.[0]
      expect(paused).toMatchObject({ status: 'paused', attempt: 3, resume: expect.any(Function) })
      expect(state.onRecovery.mock.calls.map(([entry]) => entry?.attempt)).toEqual([1, 2, 3, 3])
      if (mode === 'manual') paused?.resume?.()
      else window.dispatchEvent(new Event('online'))
      await vi.advanceTimersByTimeAsync(0)
      await expect(result).resolves.toMatchObject({ status: 'compiled' })
      paused?.resume?.() // A stale button must not start another query.
      window.dispatchEvent(new Event('online'))
      await vi.advanceTimersByTimeAsync(10_000)
      expect(state.queries()).toBe(5)
      state.expectOnePost()
      expect(vi.getTimerCount()).toBe(0)
    })

    it.each(['retrying', 'paused'])('aborts %s recovery and cleans up its resume listener', async mode => {
      const state = await begin(async () => { throw new TypeError('offline') })
      const rejected = expect(state.result).rejects.toMatchObject({ name: 'AbortError' })
      await vi.advanceTimersByTimeAsync(mode === 'paused' ? 8_000 : 1_000)
      const resume = state.onRecovery.mock.calls.at(-1)?.[0]?.resume
      const before = state.queries()
      state.controller.abort()
      await rejected
      resume?.()
      window.dispatchEvent(new Event('online'))
      await vi.advanceTimersByTimeAsync(10_000)
      expect(state.queries()).toBe(before)
      expect(state.onRecovery).toHaveBeenLastCalledWith(null)
      expect(vi.getTimerCount()).toBe(0)
      state.expectOnePost()
    })

    it('retries a bounded GET timeout while retaining the original accepted task', async () => {
      const state = await begin(async (attempt, signal) => {
        if (attempt > 1) return ready()
        return new Promise((_resolve, reject) => signal.addEventListener('abort', () => reject(signal.reason)))
      })
      const result = expect(state.result).resolves.toMatchObject({ status: 'compiled' })
      await vi.advanceTimersByTimeAsync(21_000)
      expect(state.onRecovery).toHaveBeenLastCalledWith(expect.objectContaining({
        status: 'retrying', message: expect.stringContaining('结果查询响应超时'),
      }))
      await vi.advanceTimersByTimeAsync(1_000)
      await result
      expect(state.queries()).toBe(2)
      state.expectOnePost()
    })

    it('preserves the HTTP reason while paused and resumes the same accepted request', async () => {
      const state = await begin(async attempt => attempt > 4 ? ready()
        : new Response('<html>gateway unavailable</html>', { status: 503 }))
      await vi.advanceTimersByTimeAsync(8_000)
      const paused = state.onRecovery.mock.calls.at(-1)?.[0]
      expect(paused).toMatchObject({ status: 'paused', attempt: 3,
        message: expect.stringContaining('HTTP 503'), resume: expect.any(Function) })
      expect(paused?.message).not.toContain('连接')
      expect(state.queries()).toBe(4)
      paused?.resume?.()
      await vi.advanceTimersByTimeAsync(0)
      await expect(state.result).resolves.toMatchObject({ status: 'compiled' })
      expect(state.queries()).toBe(5)
      state.expectOnePost()
      expect(vi.getTimerCount()).toBe(0)
    })

    it.each([408, 429, 502, 503, 504, 'broken-body'] as const)('retries transient %s results', async status => {
      const state = await begin(async attempt => attempt > 1 ? ready()
        : new Response('<html>gateway unavailable</html>', { status: status === 'broken-body' ? 201 : status }))
      const result = expect(state.result).resolves.toMatchObject({ status: 'compiled' })
      await vi.advanceTimersByTimeAsync(2_000)
      await result
      expect(state.queries()).toBe(2)
      state.expectOnePost()
      const retryMessage = state.onRecovery.mock.calls.find(([entry]) => entry?.status === 'retrying')?.[0]?.message
      expect(retryMessage).toContain(status === 'broken-body' ? '格式无效' : `HTTP ${status}`)
      if (status === 429) expect(retryMessage).toContain('频率受限')
      expect(retryMessage).not.toContain('连接')
    })

    it.each([404, 410, 422, 503, 504])('does not retry terminal HTTP %s business errors', async status => {
      const state = await begin(async () => new Response(JSON.stringify({
        code: 'terminal_fixture', detail: '后台已明确结束这次请求',
      }), { status }))
      const result = expect(state.result).rejects.toMatchObject({ problem: { status, code: 'terminal_fixture' } })
      await vi.advanceTimersByTimeAsync(1_000)
      await result
      expect(state.queries()).toBe(1)
      expect(state.onRecovery.mock.calls.some(([entry]) => entry?.status === 'retrying')).toBe(false)
      const events = state.onProgress.mock.calls.at(-1)?.[0]
      expect(events?.at(-1)?.stage).toBe('failed')
      expect(events?.[0]?.reasoning).toBe('真实返回的说明，不应因连接恢复而丢失。')
      state.expectOnePost()
    })

    it('fails after three automatic retries when no recovery UI was supplied', async () => {
      const state = await begin(async () => { throw new TypeError('offline') }, false)
      const result = expect(state.result).rejects.toMatchObject({ problem: { code: 'preview_poll_interrupted' } })
      await vi.advanceTimersByTimeAsync(8_000)
      await result
      expect(state.queries()).toBe(4)
      state.expectOnePost()
      expect(vi.getTimerCount()).toBe(0)
    })
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    vi.resetModules()
  })

  it('sends execution settings only with an existing conversation parent', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const bodies: Array<Record<string, unknown>> = []
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input) === '/api/v1/capabilities') {
        return { ok: true, json: async () => capabilities() }
      }
      if (String(input) === '/api/v1/strategy-drafts') {
        bodies.push(JSON.parse(String(init?.body)))
        return { ok: true, json: async () => readyResponse() }
      }
      throw new Error(`unexpected fetch: ${String(input)}`)
    }))
    const { strategyApi } = await import('./client')
    const input = { ...clarifiedRequest, executionSettings: {
      slippageBps: 2, commissionRate: 0, minimumCommissionCny: 0,
    } }
    await strategyApi.compile(input)
    await strategyApi.compile(input, 'current-parent')
    expect(bodies[0]).not.toHaveProperty('execution_settings')
    expect(bodies[1]?.execution_settings).toEqual({
      slippage_bps: 2, commission_rate: 0, minimum_commission_cny: 0,
    })
  })

  it('attaches a progress id and keeps only the latest twelve backend steps with bounded reasoning', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const progressId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    vi.stubGlobal('crypto', { randomUUID: () => progressId })
    const updates: Array<Array<{
      stage: string
      message: string
      elapsedMs: number
      reasoning?: string
      reasoningTruncated?: boolean
    }>> = []
    const boundaryReasoning = `${'r'.repeat(59_996)}tail`
    let releaseDraft: (() => void) | undefined
    const draftGate = new Promise<void>((resolve) => { releaseDraft = resolve })
    let compileProgressHeader: string | null = null

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === `/api/v1/dialogue-progress/${progressId}`) {
        return {
          ok: true,
          status: 200,
          json: async () => ({
            events: Array.from({ length: 15 }, (_, index) => ({
              stage: `stage-${index + 1}`,
              message: `真实步骤 ${index + 1}`,
              elapsed_ms: index * 1_000,
              ...(index === 13 ? {
                reasoning: boundaryReasoning,
                reasoning_truncated: false,
              } : {}),
              ...(index === 14 ? {
                reasoning: `discarded-prefix|${boundaryReasoning}`,
                reasoning_truncated: true,
              } : {}),
            })),
            finished: true,
          }),
        }
      }
      if (path === '/api/v1/capabilities') {
        return { ok: true, status: 200, json: async () => capabilities() }
      }
      if (path === '/api/v1/strategy-drafts') {
        compileProgressHeader = new Headers(init?.headers).get('X-Dialogue-Progress-ID')
        expect(new Headers(init?.headers).get('Idempotency-Key')).toBe(`draft:${progressId}`)
        await draftGate
        return { ok: true, status: 201, json: async () => readyResponse() }
      }
      throw new Error(`unexpected fetch: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { strategyApi } = await import('./client')
    const compile = strategyApi.compile({
      ...clarifiedRequest,
      dialogueProgress: {
        onProgress: (events) => updates.push([...events]),
      },
    })

    await vi.waitFor(() => expect(updates).toHaveLength(1))
    releaseDraft?.()
    await compile

    expect(compileProgressHeader).toBe(progressId)
    const requestedPaths = fetchMock.mock.calls.map(([url]) => String(url))
    expect(requestedPaths.indexOf('/api/v1/strategy-drafts')).toBeLessThan(
      requestedPaths.indexOf(`/api/v1/dialogue-progress/${progressId}`),
    )
    expect(updates[0]).toHaveLength(12)
    expect(updates[0]?.map((event) => event.stage)).toEqual(
      Array.from({ length: 12 }, (_, index) => `stage-${index + 4}`),
    )
    expect(updates[0]?.[0]).toEqual({
      stage: 'stage-4',
      message: '真实步骤 4',
      elapsedMs: 3_000,
    })
    expect(updates[0]?.[10]).toEqual({
      stage: 'stage-14',
      message: '真实步骤 14',
      elapsedMs: 13_000,
      reasoning: boundaryReasoning,
      reasoningTruncated: false,
    })
    expect(updates[0]?.[11]).toEqual({
      stage: 'stage-15',
      message: '真实步骤 15',
      elapsedMs: 14_000,
      reasoning: boundaryReasoning,
      reasoningTruncated: true,
    })
    expect(updates[0]?.[11]?.reasoning).toHaveLength(60_000)
  })

  describe('final dialogue progress snapshot', () => {
    const progressId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'
    const completed = { finished: true, events: [
      { stage: 'complete', message: '本轮处理已结束', elapsed_ms: 2_000 },
    ] }
    const begin = async (
      finalRead: (signal: AbortSignal) => Promise<Response>,
      signal?: AbortSignal,
      failDraft = false,
    ) => {
      vi.stubEnv('VITE_USE_MOCK', 'false')
      vi.stubGlobal('crypto', { randomUUID: () => progressId })
      let releaseDraft!: () => void
      const draftGate = new Promise<void>(resolve => { releaseDraft = resolve })
      let progressReads = 0
      const onProgress = vi.fn()
      const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input)
        if (path === '/api/v1/capabilities') return new Response(JSON.stringify(capabilities()))
        if (path === '/api/v1/strategy-drafts') {
          await draftGate
          return failDraft
            ? new Response(JSON.stringify({ code: 'original_failure', detail: '原始错误' }), { status: 503 })
            : new Response(JSON.stringify(readyResponse()), { status: 201 })
        }
        expect(path).toBe(`/api/v1/dialogue-progress/${progressId}`)
        progressReads += 1
        if (progressReads === 1) return new Response(JSON.stringify({ finished: false, events: [
          { stage: 'model', message: '模型正在生成', elapsed_ms: 1_000 },
        ] }))
        return finalRead(init!.signal as AbortSignal)
      })
      vi.stubGlobal('fetch', fetcher)
      const { strategyApi } = await import('./client')
      const compile = strategyApi.compile({
        ...clarifiedRequest, dialogueProgress: { onProgress, signal },
      })
      await vi.waitFor(() => expect(onProgress).toHaveBeenCalledTimes(1))
      return { compile, releaseDraft, onProgress, fetcher, reads: () => progressReads }
    }

    it('reads the terminal snapshot once when the main response arrives between polls', async () => {
      const state = await begin(async () => new Response(JSON.stringify(completed)))
      state.releaseDraft()
      await state.compile
      expect(state.onProgress).toHaveBeenLastCalledWith([
        { stage: 'complete', message: '本轮处理已结束', elapsedMs: 2_000 },
      ])
      expect(state.reads()).toBe(2)
      expect(state.fetcher.mock.calls.filter(([url]) => String(url) === '/api/v1/strategy-drafts')).toHaveLength(1)
    })

    it.each([false, true])('does not replace the main result when final GET fails (main failure: %s)', async (failDraft) => {
      const state = await begin(async () => { throw new Error('progress unavailable') }, undefined, failDraft)
      const result = failDraft
        ? expect(state.compile).rejects.toMatchObject({ problem: { code: 'original_failure' } })
        : expect(state.compile).resolves.toBeDefined()
      state.releaseDraft()
      await result
      expect(state.onProgress).toHaveBeenCalledTimes(failDraft ? 2 : 1)
      expect(state.reads()).toBe(2)
    })

    it('limits the final GET to one second without a new AbortSignal timeout dependency', async () => {
      let finalSignal: AbortSignal | undefined
      const state = await begin(signal => {
        finalSignal = signal
        return new Promise((_resolve, reject) => signal.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'))
        }, { once: true }))
      })
      vi.useFakeTimers()
      try {
        state.releaseDraft()
        await vi.advanceTimersByTimeAsync(0)
        expect(finalSignal?.aborted).toBe(false)
        await vi.advanceTimersByTimeAsync(1_000)
        await expect(state.compile).resolves.toBeDefined()
        expect(finalSignal?.aborted).toBe(true)
        expect(state.reads()).toBe(2)
        expect(state.onProgress).toHaveBeenCalledTimes(1)
      } finally {
        vi.useRealTimers()
      }
    })

    it.each(['deadline', 'cancel'])('cancels a stalled final response body on %s after headers arrive', async mode => {
      const controller = new AbortController()
      let finalSignal: AbortSignal | undefined
      let finalResponse: Response | undefined
      const state = await begin(async signal => {
        finalSignal = signal
        finalResponse = new Response(new ReadableStream({ start(stream) {
          signal.addEventListener('abort', () => stream.error(signal.reason), { once: true })
        } }), { headers: { 'Content-Type': 'application/json' } })
        return finalResponse
      }, controller.signal)
      vi.useFakeTimers()
      try {
        state.releaseDraft()
        await vi.advanceTimersByTimeAsync(0)
        expect(finalResponse?.bodyUsed).toBe(true)
        expect(finalSignal?.aborted).toBe(false)
        if (mode === 'cancel') controller.abort()
        else await vi.advanceTimersByTimeAsync(1_000)
        await expect(state.compile).resolves.toBeDefined()
        expect(finalSignal?.aborted).toBe(true)
        expect(state.onProgress).toHaveBeenCalledTimes(1)
        expect(vi.getTimerCount()).toBe(0)
      } finally { vi.useRealTimers() }
    })

    it('skips the final GET when the observer was cancelled for another turn', async () => {
      const controller = new AbortController()
      const finalRead = vi.fn(async () => new Response(JSON.stringify(completed)))
      const state = await begin(finalRead, controller.signal)
      controller.abort()
      state.releaseDraft()
      await state.compile
      expect(finalRead).not.toHaveBeenCalled()
      expect(state.onProgress).toHaveBeenCalledTimes(1)
    })

    it('ignores a late final response after its observer is cancelled', async () => {
      const controller = new AbortController()
      let releaseFinal!: () => void
      const finalGate = new Promise<void>(resolve => { releaseFinal = resolve })
      const finalRead = vi.fn(async () => {
        await finalGate
        return new Response(JSON.stringify(completed))
      })
      const state = await begin(finalRead, controller.signal)
      state.releaseDraft()
      await vi.waitFor(() => expect(finalRead).toHaveBeenCalledTimes(1))
      controller.abort()
      releaseFinal()
      await state.compile
      expect(state.onProgress).toHaveBeenCalledTimes(1)
      expect(state.reads()).toBe(2)
    })
  })

  it('allows the server-owned snapshot preparation step to outlive ordinary API calls', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const supported = capabilities({
      event_backtest_available: true,
      event_availability_scope: 'pinned_snapshot',
      events: [{
        event_code: 'event.financial_results.annual_report',
        definition_version: '1.0.0',
        catalog_status: 'stable',
        status: 'available',
        backtest_available: true,
        preparation_available: false,
        availability_scope: 'pinned_snapshot',
        unavailable_reason: null,
        triggers: ['published'],
      }],
    })
    const timeoutSpy = vi.spyOn(window, 'setTimeout')
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      void _init
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        return { ok: true, json: async () => supported }
      }
      if (path === '/api/v1/strategy-drafts') {
        return { ok: true, json: async () => readyResponse() }
      }
      if (path === '/api/v1/backtest-runs') {
        return { ok: true, json: async () => ({ id: 'run:prepared', state: 'queued' }) }
      }
      throw new Error(`unexpected fetch: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { backtestApi, strategyApi } = await import('./client')
    const compiled = await strategyApi.compile({
      ...clarifiedRequest,
      utterance: '东方财富年报发布后买入，MACD 死叉卖出',
    })
    if (compiled.status !== 'compiled') throw new Error('expected compiled StrategySpec')
    expect(timeoutSpy).not.toHaveBeenCalledWith(expect.any(Function), 3_600_000)
    const draftCall = fetchMock.mock.calls.find(([path]) => path === '/api/v1/strategy-drafts')
    expect((draftCall?.[1] as RequestInit | undefined)?.signal?.aborted).toBe(false)

    await backtestApi.create(compiled.draft)

    expect(timeoutSpy).toHaveBeenLastCalledWith(expect.any(Function), 300_000)
  })

  it('submits a reviewed server-compiled strategy directly as a fresh run', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const timeoutSpy = vi.spyOn(window, 'setTimeout')
    const supported = capabilities({
      event_backtest_available: true,
      event_availability_scope: 'pinned_snapshot',
      events: [{
        event_code: 'event.financial_results.annual_report',
        definition_version: '1.0.0',
        catalog_status: 'stable',
        status: 'available',
        backtest_available: true,
        preparation_available: false,
        availability_scope: 'pinned_snapshot',
        unavailable_reason: null,
        triggers: ['published'],
      }],
    })
    const optimizedStrategy: StrategySpec = {
      ...eventStrategy,
      exit: {
        op: 'first_of',
        children: [{
          type: 'holding_period_exit',
          sessions: 10,
          anchor: 'first_entry_fill',
          count_mode: 'subsequent_trading_sessions',
          execution: 'target_session_open_proxy',
        }],
      },
    }
    const reviewResponse: BacktestReviewResponse = {
      runId: 'run:base',
      sourceResultHash: `sha256:${'b'.repeat(64)}`,
      generatedAt: '2026-09-05T12:00:00Z',
      evidenceGrade: 'limited',
      evidenceReasons: ['有效交易样本偏少'],
      analysis: '策略在震荡区间有多次往返交易。',
      conclusion: '需要独立检验固定持有期。',
      optimizationCandidates: [{
        id: 'model-opt-1',
        title: '固定持有 10 日',
        diagnosis: '原卖出信号在震荡期反复触发。',
        changeDimension: 'exit',
        expectedEffect: '检验固定持有期是否降低往返交易。',
        tradeoff: '可能错过更早的风险退出信号。',
        suggestedUtterance: '东方财富年报发布后买入，实际成交后第 10 个交易日卖出',
        strategy: optimizedStrategy,
        strategyHash: `sha256:${'c'.repeat(64)}`,
        modelSuggested: true,
      }, {
        id: 'model-opt-2',
        title: '放慢退出确认',
        diagnosis: '原卖出信号可能对短期波动过敏。',
        changeDimension: 'confirmation',
        expectedEffect: '检验更慢确认是否减少假信号。',
        tradeoff: '确认变慢可能放大单笔回撤。',
        suggestedUtterance: '东方财富年报发布后买入，实际成交后第 10 个交易日卖出',
        strategy: optimizedStrategy,
        strategyHash: `sha256:${'d'.repeat(64)}`,
        modelSuggested: true,
      }],
      modelProvenance: {
        provider: 'deepseek',
        model: 'deepseek-v4-pro',
        promptVersion: 'backtest-review.prompt.v1',
        schemaVersion: 'backtest-review.v1',
        responseHash: `sha256:${'e'.repeat(64)}`,
      },
      disclaimer: '历史回测与模型建议仅用于研究，不构成投资建议或真实交易指令',
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        return { ok: true, status: 200, json: async () => supported }
      }
      if (path === '/api/v1/strategy-drafts') {
        return { ok: true, status: 200, json: async () => readyResponse() }
      }
      if (path === '/api/v1/backtest-runs/run%3Abase/review') {
        expect(init?.method).toBe('POST')
        expect(init?.body).toBeUndefined()
        return { ok: true, status: 200, json: async () => reviewResponse }
      }
      if (path.endsWith('/revisions')) {
        expect(JSON.parse(String(init?.body)).strategy).toEqual(optimizedStrategy)
        expect(JSON.parse(String(init?.body)).recover_if_missing).toBe(true)
        return { ok: true, status: 201, json: async () => ({
          ...readyResponse(), draft_id: 'recovered-optimization-draft',
          revision: 1, strategy: optimizedStrategy,
          strategy_hash: `sha256:${'c'.repeat(64)}`,
        }) }
      }
      if (path === '/api/v1/backtest-runs') {
        return {
          ok: true,
          status: 202,
          json: async () => ({
            id: 'run:optimized',
            state: 'queued',
            progress: 0,
            progressLabel: '已排队',
            createdAt: '2026-09-05T12:01:00Z',
            updatedAt: '2026-09-05T12:01:00Z',
            fingerprint: 'fresh-optimization',
            error: null,
            resultAvailable: false,
          }),
        }
      }
      throw new Error(`unexpected fetch: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { backtestApi, strategyApi } = await import('./client')
    const compiled = await strategyApi.compile({
      ...clarifiedRequest,
      utterance: '东方财富年报发布后买入，MACD 死叉卖出',
    })
    if (compiled.status !== 'compiled') throw new Error('expected compiled StrategySpec')

    const reviewed = await backtestApi.review('run:base')
    const optimized = await backtestApi.createOptimization(
      reviewed.optimizationCandidates[0]!,
      compiled.draft,
    )

    expect(optimized.run.id).toBe('run:optimized')
    expect(optimized.draft.strategySpec).toEqual(optimizedStrategy)
    expect(optimized.draft.strategyHash).toBe(`sha256:${'c'.repeat(64)}`)
    expect(optimized.draft.sourceText).toBe(reviewed.optimizationCandidates[0]?.suggestedUtterance)
    expect(optimized.draft.execution).toEqual(compiled.draft.execution)
    const createCall = fetchMock.mock.calls.find(([path]) => path === '/api/v1/backtest-runs')
    expect(createCall).toBeDefined()
    const body = JSON.parse(String((createCall?.[1] as RequestInit).body))
    expect(body.strategy).toEqual(optimizedStrategy)
    expect(body.config).toMatchObject({
      capacityMode: compiled.draft.execution.capacityMode,
      allocationRatio: compiled.draft.execution.allocationRatio,
      slippageBps: compiled.draft.execution.slippageBps,
    })
    expect(fetchMock.mock.calls.filter(([path]) => path === '/api/v1/strategy-drafts')).toHaveLength(1)
    expect(optimized.draft.id).toBe('recovered-optimization-draft')
    expect(optimized.draft.revision).toBe(1)
    const saveIndex = fetchMock.mock.calls.findIndex(([path]) => String(path).endsWith('/revisions'))
    const runIndex = fetchMock.mock.calls.findIndex(([path]) => path === '/api/v1/backtest-runs')
    expect(saveIndex).toBeGreaterThan(-1)
    expect(saveIndex).toBeLessThan(runIndex)
    expect(timeoutSpy).toHaveBeenCalledWith(expect.any(Function), 300_000)
  })

  it('posts bounded report and exposed review references without browser metrics', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const runIds = Array.from({ length: 23 }, (_, index) => `run:${index}`)
    const fetchMock = vi.fn(async () => ({ ok: true, status: 200, json: async () => ({}) }))
    vi.stubGlobal('fetch', fetchMock)
    const { backtestApi } = await import('./client')
    await backtestApi.review('run:22', undefined, {
      relatedRunIds: runIds,
      relatedReviews: [
        { runId: 'run:0', responseHash: 'sha256:outside-window' },
        { runId: 'run:21', responseHash: 'sha256:first-version' },
        { runId: 'run:21', responseHash: 'sha256:second-version' },
      ],
    })
    expect(fetchMock).toHaveBeenCalledOnce()
    const [path, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit]
    expect(path).toBe('/api/v1/backtest-runs/run%3A22/review')
    expect(init.method).toBe('POST')
    expect(JSON.parse(String(init.body))).toEqual({
      related_run_ids: runIds.slice(-20),
      related_reviews: [
        { run_id: 'run:21', response_hash: 'sha256:first-version' },
        { run_id: 'run:21', response_hash: 'sha256:second-version' },
      ],
    })
  })

  it('fails closed instead of fabricating an AI review in Mock mode', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'true')
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)

    const { backtestApi } = await import('./client')

    await expect(backtestApi.review('run:mock')).rejects.toMatchObject({
      problem: expect.objectContaining({ code: 'backtest_review_model_unavailable' }),
    })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it.each([
    [true, undefined],
    [false, 'true'],
  ])('uses the real API when DEV=%s and preview flag=%s', async (dev, flag) => {
    vi.stubEnv('DEV', dev)
    vi.stubEnv('VITE_USE_MOCK', flag)
    const { apiMode } = await import('./client')
    expect(apiMode).toBe('live')
  })

  it('sends verified stock-page context through the backend instrument_context field', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    vi.stubEnv('VITE_DATA_AS_OF_DATE', '2026-08-06')
    const fetchMock = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
      void _init
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        return { ok: true, json: async () => capabilities() }
      }
      if (path === '/api/v1/strategy-drafts') {
        return {
          ok: true,
          json: async () => ({
            draft_id: '8c91eb84-ab49-4b0c-890a-682e9cc6fe21',
            revision: 1,
            status: 'needs_clarification',
            strategy: null,
            strategy_hash: null,
            clarification: '请确认要回测的 A 股。',
            diagnostic_code: 'instrument_required',
            provenance: [],
            created_at: '2026-08-29T00:00:00Z',
          }),
        }
      }
      throw new Error(`unexpected fetch: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { strategyApi } = await import('./client')
    await strategyApi.compile(clarifiedRequest)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    const capabilityCall = fetchMock.mock.calls.find(([path]) => path === '/api/v1/capabilities')
    expect(capabilityCall).toBeDefined()
    expect((capabilityCall?.[1] as RequestInit).cache).toBe('no-store')
    const draftCall = fetchMock.mock.calls.find(([path]) => path === '/api/v1/strategy-drafts')
    expect(draftCall).toBeDefined()
    const [path, init] = draftCall as unknown as [string, RequestInit]
    expect(path).toBe('/api/v1/strategy-drafts')
    expect(JSON.parse(String(init.body))).toEqual({
      utterance: clarifiedRequest.utterance,
      instrument_context: '300059.SZ',
      as_of_date: '2026-08-06',
    })
  })

  it('answers a saved draft revision with execution settings without rebuilding the rule in the browser', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const clarificationDraft: LiveDraftResponse = {
      draft_id: '8c91eb84-ab49-4b0c-890a-682e9cc6fe21',
      revision: 3,
      status: 'needs_clarification',
      strategy: null,
      strategy_hash: null,
      clarification: '已识别卖出条件。请补充买入条件。',
      diagnostic_code: 'entry_rule_not_recognized',
      execution_settings: { slippage_bps: '2', commission_rate: '0', minimum_commission_cny: '0' },
      provenance: [],
      candidate_provenance: null,
      candidate_grounding: null,
      candidate_alternatives: [],
      candidate_rejections: [],
      created_at: '2026-08-29T00:00:00Z',
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        return { ok: true, json: async () => capabilities() }
      }
      if (path === '/api/v1/strategy-drafts') {
        return { ok: true, json: async () => clarificationDraft }
      }
      if (path === '/api/v1/strategy-drafts/8c91eb84-ab49-4b0c-890a-682e9cc6fe21/revisions/3/clarification-answers') {
        expect(init?.method).toBe('POST')
        expect(JSON.parse(String(init?.body))).toEqual({
          answer: 'RSI 低于 30 买入', related_run_ids: ['run:old', 'run:previous', 'run:current'],
          related_review: { run_id: 'run:current', response_hash: 'sha256:current' },
          related_reviews: [
            { run_id: 'run:old', response_hash: 'sha256:old' },
            { run_id: 'run:current', response_hash: 'sha256:current' },
          ],
          execution_settings: { slippage_bps: 2, commission_rate: 0, minimum_commission_cny: 0 },
        })
        expect(new Headers(init?.headers).has('Idempotency-Key')).toBe(false)
        return {
          ok: true,
          json: async () => ({
            reply_kind: 'clarification',
            assistant_message: '已保留 MACD 死叉卖出，请确认 RSI 阈值。',
            suggestions: [{
              id: 'idea_123456789abc',
              title: 'RSI 低于 30 买入',
              preview: 'RSI 低于 30 买入，MACD 死叉卖出',
            }],
            draft: clarificationDraft,
          }),
        }
      }
      throw new Error(`unexpected fetch: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { strategyApi } = await import('./client')
    const pending = await strategyApi.compile({
      ...clarifiedRequest,
      utterance: 'MACD 死叉卖出',
    })
    if (pending.status !== 'needs_clarification') throw new Error('expected clarification')

    const answer = await strategyApi.answerClarification({
      draftId: pending.draftId,
      revision: pending.revision,
      answer: 'RSI 低于 30 买入',
      executionSettings: pending.executionSettings,
      relatedRunIds: ['run:old', 'run:previous', 'run:current'],
      relatedReview: { runId: 'run:current', responseHash: 'sha256:current' },
      relatedReviews: [
        { runId: 'run:old', responseHash: 'sha256:old' },
        { runId: 'run:current', responseHash: 'sha256:current' },
      ],
      originalRequest: { ...clarifiedRequest, utterance: 'MACD 死叉卖出' },
      clarification: pending.clarification,
    })

    expect(answer).toMatchObject({
      replyKind: 'clarification',
      assistantMessage: '已保留 MACD 死叉卖出，请确认 RSI 阈值。',
      suggestions: [{ id: 'idea_123456789abc' }],
      outcome: { status: 'needs_clarification', draftId: clarificationDraft.draft_id, revision: 3,
        executionSettings: { slippageBps: 2, commissionRate: 0, minimumCommissionCny: 0 } },
    })
  })

  it('compiles a server-owned StrategySpec even when execution is unavailable, then gates revise and run', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const unavailable = capabilities({
      backtest_execution_available: false,
      events: [{
        event_code: 'event.financial_results.annual_report',
        definition_version: '1.0.0',
        catalog_status: 'stable',
        status: 'unavailable',
        backtest_available: false,
        preparation_available: false,
        availability_scope: 'unavailable',
        unavailable_reason: 'snapshot_coverage_unavailable',
        triggers: ['published'],
      }],
    })
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        return { ok: true, json: async () => unavailable }
      }
      if (path === '/api/v1/strategy-drafts') {
        return { ok: true, json: async () => readyResponse() }
      }
      throw new Error(`execution endpoint must not be called: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { backtestApi, strategyApi } = await import('./client')
    const compiled = await strategyApi.compile({
      ...clarifiedRequest,
      utterance: '东方财富年报发布后买入，MACD 死叉卖出',
    })

    expect(compiled.status).toBe('compiled')
    if (compiled.status !== 'compiled') throw new Error('expected compiled StrategySpec')
    expect(compiled.draft.strategySpec).toEqual(eventStrategy)
    expect(compiled.draft.entry.conditions[0]).toMatchObject({
      eventCode: 'event.financial_results.annual_report',
      label: '年度报告发布',
    })

    await expect(strategyApi.revise(compiled.draft)).rejects.toMatchObject({
      problem: expect.objectContaining({ code: 'backtest_service_unavailable' }),
    })
    await expect(backtestApi.create(compiled.draft)).rejects.toMatchObject({
      problem: expect.objectContaining({ code: 'backtest_service_unavailable' }),
    })
    expect(fetchMock.mock.calls.filter(([path]) => path === '/api/v1/strategy-drafts')).toHaveLength(1)
    expect(fetchMock.mock.calls.some(([path]) => String(path).includes('/revisions'))).toBe(false)
    expect(fetchMock.mock.calls.some(([path]) => path === '/api/v1/backtest-runs')).toBe(false)
  })

  it('does not turn an optional capability-metadata outage into a compile failure', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        return {
          ok: false,
          status: 503,
          json: async () => ({
            error: { code: 'capabilities_unavailable', message: '能力元数据暂不可用' },
            request_id: 'request-capabilities-503',
          }),
        }
      }
      if (path === '/api/v1/strategy-drafts') {
        return { ok: true, json: async () => readyResponse() }
      }
      throw new Error(`unexpected fetch: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    const { strategyApi } = await import('./client')
    const result = await strategyApi.compile({
      ...clarifiedRequest,
      utterance: '东方财富年报发布后买入，MACD 死叉卖出',
    })

    expect(result.status).toBe('compiled')
    if (result.status !== 'compiled') throw new Error('expected compiled StrategySpec')
    expect(result.draft.strategySpec).toEqual(eventStrategy)
    expect(result.draft.entry.conditions[0]).toMatchObject({
      eventCode: 'event.financial_results.annual_report',
      label: '年度报告发布',
    })
  })

  it('fails closed when an event is neither snapshot-backed nor request-preparable', async () => {
    const { requireStrategyCapability } = await import('./client')

    expect(() => requireStrategyCapability(eventStrategy, capabilities())).toThrow(
      '当前快照没有这类事件',
    )
  })

  it('accepts a truthful on-demand preparation capability without calling it snapshot-backed', async () => {
    const { requireStrategyCapability } = await import('./client')
    const onDemand = capabilities({
      event_preparation_available: true,
      event_availability_scope: 'request_preparation',
      events: [{
        event_code: 'event.financial_results.annual_report',
        definition_version: '1.0.0',
        catalog_status: 'stable',
        status: 'unavailable',
        backtest_available: false,
        preparation_available: true,
        availability_scope: 'request_preparation',
        unavailable_reason: 'preparation_required',
        triggers: ['published'],
      }],
    })

    expect(() => requireStrategyCapability(eventStrategy, onDemand)).not.toThrow()
    expect(onDemand.events[0]?.backtest_available).toBe(false)
  })

  it('fails closed when the event is snapshot-backed but required document text is unavailable', async () => {
    const { requireStrategyCapability } = await import('./client')
    const eventOnly = capabilities({
      events: [{
        event_code: 'event.financial_results.annual_report',
        definition_version: '1.0.0',
        catalog_status: 'stable',
        status: 'available',
        backtest_available: true,
        preparation_available: false,
        availability_scope: 'pinned_snapshot',
        unavailable_reason: null,
        document_text: {
          catalog_available: true,
          backtest_available: false,
          preparation_available: false,
          availability_scope: 'unavailable',
          unavailable_reason: 'snapshot_coverage_unavailable',
        },
        triggers: ['published'],
      }],
    })

    expect(() => requireStrategyCapability(documentTextStrategy, eventOnly)).toThrow(
      '需要公告完整正文与词频数据',
    )
  })

  it('accepts document text only when its own capability is request-preparable', async () => {
    const { requireStrategyCapability } = await import('./client')
    const preparableDocument = capabilities({
      events: [{
        event_code: 'event.financial_results.annual_report',
        definition_version: '1.0.0',
        catalog_status: 'stable',
        status: 'available',
        backtest_available: true,
        preparation_available: false,
        availability_scope: 'pinned_snapshot',
        unavailable_reason: null,
        document_text: {
          catalog_available: true,
          backtest_available: false,
          preparation_available: true,
          availability_scope: 'request_preparation',
          unavailable_reason: 'preparation_required',
        },
        triggers: ['published'],
      }],
    })

    expect(() => requireStrategyCapability(documentTextStrategy, preparableDocument)).not.toThrow()
  })

  it('does not require document text for an ordinary event-only strategy', async () => {
    const { requireStrategyCapability } = await import('./client')
    const eventOnly = capabilities({
      events: [{
        event_code: 'event.financial_results.annual_report',
        definition_version: '1.0.0',
        catalog_status: 'stable',
        status: 'available',
        backtest_available: true,
        preparation_available: false,
        availability_scope: 'pinned_snapshot',
        unavailable_reason: null,
        document_text: {
          catalog_available: true,
          backtest_available: false,
          preparation_available: false,
          availability_scope: 'unavailable',
          unavailable_reason: 'snapshot_coverage_unavailable',
        },
        triggers: ['published'],
      }],
    })

    expect(() => requireStrategyCapability(eventStrategy, eventOnly)).not.toThrow()
  })

  it('calls the configured public HTTPS API without falling back to Mock', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.cn')
    const fetchMock = vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => capabilities(),
    }))
    vi.stubGlobal('fetch', fetchMock)

    const { apiMode, systemApi } = await import('./client')
    await systemApi.capabilities()

    expect(apiMode).toBe('live')
    expect(fetchMock).toHaveBeenCalledWith(
      'https://api.example.cn/api/v1/capabilities',
      expect.objectContaining({ cache: 'no-store', signal: expect.any(AbortSignal) }),
    )
  })

  it('turns a browser network failure into an actionable Live error', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    vi.stubEnv('VITE_API_BASE_URL', 'https://api.example.cn')
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }))

    const { systemApi } = await import('./client')

    await expect(systemApi.capabilities()).rejects.toMatchObject({
      problem: expect.objectContaining({
        code: 'api_network_unavailable',
        detail: expect.stringContaining('网络异常'),
      }),
    })
  })

  it('rejects a successful HTTP response that is not valid JSON', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      status: 200,
      json: async () => { throw new SyntaxError('unexpected token') },
    })))

    const { systemApi } = await import('./client')

    await expect(systemApi.capabilities()).rejects.toMatchObject({
      problem: expect.objectContaining({ code: 'api_invalid_json' }),
    })
  })

  it('preserves well-formed backend failure locations without accepting malformed details', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: false, status: 422,
      json: async () => ({ error: { code: 'skill_indicator_history_not_ready', message: '未准备好',
        details: [null, { message: 42 }, {
          location: '/exit/children/1', message: '缺少有效历史值', type: 'backtest_condition_unavailable',
        }],
      }, request_id: 'safe-request' }),
    })))
    const { systemApi } = await import('./client')
    await expect(systemApi.capabilities()).rejects.toMatchObject({ problem: {
      code: 'skill_indicator_history_not_ready', requestId: 'safe-request',
      details: [{ location: '/exit/children/1', message: '缺少有效历史值', type: 'backtest_condition_unavailable' }],
    } })
  })
})
