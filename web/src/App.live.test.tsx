import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { LiveDraftResponse } from './shared/api/contract'
import type { CapabilitiesResponse, StrategySpec } from './shared/api/types'

const technicalStrategy: StrategySpec = {
  schema_version: 'strategy.v1',
  catalog: { catalog_id: 'cn_a.signals', release_version: '2026.08.29' },
  instrument: { market: 'CN_A', symbol: '300059.SZ', position_mode: 'long_only' },
  entry: {
    type: 'indicator_condition',
    indicator_id: 'technical.macd',
    definition_version: '1.0.0',
    params: { fast: 12, slow: 26, signal: 9 },
    timeframe: '1d',
    evaluation_mode: 'bar_close_confirmed',
    trigger: 'golden_cross',
    value: null,
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
    data_capability: 'daily_ohlcv',
    execution_resolution: '1d',
    evaluation_frequency: '1d_close',
    position_policy: 'single_position_no_pyramiding',
    t_plus_one: true,
  },
  backtest: { start: '2021-08-06', end: '2026-08-06', initial_cash_cny: 1_000_000 },
}

const readyDraft: LiveDraftResponse = {
  draft_id: '8c91eb84-ab49-4b0c-890a-682e9cc6fe21',
  revision: 1,
  status: 'ready',
  strategy: technicalStrategy,
  strategy_hash: `sha256:${'a'.repeat(64)}`,
  clarification: null,
  diagnostic_code: null,
  provenance: [],
  candidate_provenance: null,
  candidate_grounding: null,
  candidate_alternatives: [],
  candidate_rejections: [],
  created_at: '2026-08-30T00:00:00Z',
}

const quarterlyPositionStrategy: StrategySpec = {
  ...technicalStrategy,
  entry: {
    type: 'event_condition',
    event_code: 'event.financial_results.quarterly_report',
    definition_version: '1.0.0',
    trigger: 'published',
    attributes: {},
  },
  exit: {
    op: 'first_of',
    children: [
      {
        type: 'position_return_exit', trigger: 'take_profit', threshold_pct: 33,
        anchor: 'first_entry_fill', observation: 'back_adjusted_daily_close',
        evaluation_mode: 'bar_close_confirmed', execution: 'next_tradable_session_open',
      },
      {
        type: 'trailing_drawdown_exit', threshold_pct: 3,
        anchor: 'first_entry_fill', peak_basis: 'back_adjusted_daily_close',
        evaluation_mode: 'bar_close_confirmed', execution: 'next_tradable_session_open',
      },
    ],
  },
  execution: {
    ...technicalStrategy.execution,
    data_capability: 'daily_ohlcv_events',
    evaluation_frequency: 'event_available_plus_1d_close',
  },
}

const quarterlyCapabilities: CapabilitiesResponse = {
  markets: ['CN_A'],
  input_modes: ['natural_language_zh'],
  strategy_scopes: ['single_instrument', 'long_only'],
  indicators: [],
  events: [{
    event_code: 'event.financial_results.quarterly_report',
    definition_version: '1.0.0',
    catalog_status: 'stable',
    status: 'available',
    backtest_available: true,
    preparation_available: false,
    availability_scope: 'pinned_snapshot',
    unavailable_reason: null,
    triggers: ['published'],
  }],
  execution_policies: ['next_tradable_session_open'],
  event_catalog_status: 'published',
  event_backtest_available: true,
  event_preparation_available: false,
  event_availability_scope: 'pinned_snapshot',
  backtest_execution_available: true,
  limits: {
    max_body_bytes: 16_384,
    max_utterance_characters: 2000,
    max_instrument_context_characters: 32,
  },
}

const executionUnavailableCapabilities: CapabilitiesResponse = {
  markets: ['CN_A'],
  input_modes: ['natural_language_zh'],
  strategy_scopes: ['single_instrument', 'long_only'],
  indicators: [{
    indicator_id: 'technical.macd',
    definition_version: '1.0.0',
    status: 'stable',
    display_name: 'MACD',
    description: 'MACD 指标',
    warmup_bars: 35,
    timeframes: ['1d'],
    evaluation_modes: ['bar_close_confirmed'],
    triggers: ['golden_cross', 'death_cross'],
    parameters: [
      { name: 'fast', value_type: 'integer', required: true, default: 12,
        minimum: 2, maximum: 200, choices: [], display_name: '快线', unit: null },
      { name: 'slow', value_type: 'integer', required: true, default: 26,
        minimum: 3, maximum: 300, choices: [], display_name: '慢线', unit: null },
      { name: 'signal', value_type: 'integer', required: true, default: 9,
        minimum: 2, maximum: 100, choices: [], display_name: '信号线', unit: null },
    ],
    trigger_definitions: [
      { id: 'golden_cross', display_name: '金叉', description: null,
        value_requirement: 'forbidden', minimum: null, maximum: null, unit: null,
        exclusive_minimum: false, exclusive_maximum: false },
      { id: 'death_cross', display_name: '死叉', description: null,
        value_requirement: 'forbidden', minimum: null, maximum: null, unit: null,
        exclusive_minimum: false, exclusive_maximum: false },
    ],
  }],
  events: [],
  execution_policies: ['next_tradable_session_open'],
  event_catalog_status: 'published',
  event_backtest_available: false,
  event_preparation_available: false,
  event_availability_scope: 'unavailable',
  backtest_execution_available: false,
  limits: {
    max_body_bytes: 16_384,
    max_utterance_characters: 2000,
    max_instrument_context_characters: 32,
  },
}

type FetchMock = ReturnType<typeof vi.fn>

const renderLiveApp = async () => {
  const [{ default: App }, { QueryClient, QueryClientProvider }] = await Promise.all([
    import('./App'),
    import('@tanstack/react-query'),
  ])
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  )
}

const submitLiveRule = async (utterance: string) => {
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('交易规则'), utterance)
  await user.click(screen.getByRole('button', { name: '识别交易规则' }))
}

const expectLiveCompileRequest = (fetchMock: FetchMock) => {
  const draftCalls = fetchMock.mock.calls.filter(([path]) =>
    String(path) === '/api/v1/strategy-drafts')
  expect(draftCalls).toHaveLength(1)
  const init = draftCalls[0]?.[1] as RequestInit
  expect(init.method).toBe('POST')
  expect(JSON.parse(String(init.body))).toMatchObject({
    utterance: '东方财富 MACD 刚金叉，而且股价也站上 20 日线了就买入；MACD 死叉就卖出，看看近 1 年效果',
    as_of_date: expect.stringMatching(/^\d{4}-\d{2}-\d{2}$/),
  })
  expect(fetchMock.mock.calls.some(([path]) => String(path).includes('/revisions'))).toBe(false)
  expect(fetchMock.mock.calls.some(([path]) => String(path) === '/api/v1/backtest-runs')).toBe(false)
}

const expectRecognizedButNotRunnable = async (fetchMock: FetchMock) => {
  expect(await screen.findByText('预览策略')).toBeInTheDocument()
  expect(screen.getAllByText('MACD 金叉').length).toBeGreaterThan(0)
  expect(screen.getAllByText('MACD 死叉').length).toBeGreaterThan(0)
  expect(document.querySelector('.app')).toHaveAttribute('data-api-mode', 'live')
  expect(screen.queryByText('界面预览')).not.toBeInTheDocument()
  expect(screen.queryByText(/固定样例/)).not.toBeInTheDocument()

  const start = screen.getByRole('button', { name: '请检查设置' })
  expect(start).toBeDisabled()
  await userEvent.setup().click(start)
  expectLiveCompileRequest(fetchMock)
}

describe('Live App capability boundary', () => {
  beforeEach(() => {
    vi.resetModules()
    vi.stubEnv('VITE_USE_MOCK', 'false')
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    vi.unstubAllEnvs()
    vi.resetModules()
  })

  it('keeps a ready StrategySpec visible when execution is unavailable', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        return { ok: true, status: 200, json: async () => executionUnavailableCapabilities }
      }
      if (path === '/api/v1/strategy-drafts') {
        return { ok: true, status: 200, json: async () => readyDraft }
      }
      throw new Error(`unexpected Live request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    await renderLiveApp()
    await submitLiveRule('东方财富 MACD 刚金叉，而且股价也站上 20 日线了就买入；MACD 死叉就卖出，看看近 1 年效果')

    await expectRecognizedButNotRunnable(fetchMock)
    expect(screen.getAllByText(
      '后端当前没有可用的回测执行环境。规则可以查看，但不能提交运行。',
    ).length).toBeGreaterThan(0)
    expect(screen.queryByRole('region', { name: '策略能力状态' })).not.toBeInTheDocument()
  })

  it('keeps a ready StrategySpec visible when capability discovery fails', async () => {
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
        return { ok: true, status: 200, json: async () => readyDraft }
      }
      throw new Error(`unexpected Live request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    await renderLiveApp()
    await submitLiveRule('东方财富 MACD 刚金叉，而且股价也站上 20 日线了就买入；MACD 死叉就卖出，看看近 1 年效果')

    await expectRecognizedButNotRunnable(fetchMock)
    expect(screen.getAllByText(/暂时读不到后端能力说明/).length).toBeGreaterThan(0)
    expect(screen.queryByRole('region', { name: '策略能力状态' })).not.toBeInTheDocument()
  })

  it('renders non-annual capability-backed events and position-aware exits without losing Live semantics', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        return { ok: true, status: 200, json: async () => quarterlyCapabilities }
      }
      if (path === '/api/v1/strategy-drafts') {
        return {
          ok: true,
          status: 200,
          json: async () => ({ ...readyDraft, strategy: quarterlyPositionStrategy }),
        }
      }
      throw new Error(`unexpected Live request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    await renderLiveApp()
    await submitLiveRule('东方财富季度报告发布后买入，收益33%止盈或高点回撤3%卖出')

    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    expect(screen.getAllByText('季度报告发布').length).toBeGreaterThan(0)
    expect(screen.getAllByText('持仓收益达到 33% 止盈').length).toBeGreaterThan(0)
    expect(screen.getAllByText('持仓后收盘高点回撤 3% 卖出').length).toBeGreaterThan(0)
    expect(screen.queryByText(/只开放年度报告/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: '开始回测' })).toBeEnabled()
    expect(screen.queryByRole('region', { name: '策略能力状态' })).not.toBeInTheDocument()

    await userEvent.setup().click(screen.getByRole('button', { name: /持仓收益达到 33% 止盈/ }))
    expect(screen.getByRole('combobox', { name: '卖出条件关系' })).toBeEnabled()
    expect(screen.getByRole('spinbutton', { name: '幅度（%）' })).toHaveValue(33)
  })
})
