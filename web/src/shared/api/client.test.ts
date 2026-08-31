import type { CapabilitiesResponse, CompileRequest, StrategySpec } from './types'
import type { LiveDraftResponse } from './contract'

const clarifiedRequest: CompileRequest = {
  utterance: '东方财富 MACD 金叉买入，死叉卖出',
  instrument: {
    name: '东方财富',
    symbol: '300059.SZ',
    market: 'CN_A',
    exchange: 'SZSE',
  },
  clarification: {
    id: 'instrument_required',
    choiceId: 'use-current-instrument',
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
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('sends the clarification answer through the backend v2 instrument_context field', async () => {
    vi.stubEnv('VITE_USE_MOCK', 'false')
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
      clarification: undefined,
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
      clarification: undefined,
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
        detail: expect.stringContaining('没有连上回测服务'),
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
})
