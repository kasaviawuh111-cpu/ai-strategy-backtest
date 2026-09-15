import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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

// Scope asynchronous control queries to their workspace. Searching the entire
// mounted gallery on each retry can starve React Query notification timers.
const reviewControls = () => within(screen.getByRole('complementary', { name: '策略审阅' }))

const renderLiveApp = async () => {
  const [{ default: App }, { QueryClient, QueryClientProvider }] = await Promise.all([
    import('./App'),
    import('@tanstack/react-query'),
  ])
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, retryDelay: 0 }, mutations: { retry: false } },
  })
  return { client, ...render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  ) }
}

const submitLiveRule = async (utterance: string) => {
  const user = userEvent.setup()
  // These cases verify API recovery, not per-keystroke editing. Submit the
  // complete input through React's change event without dozens of timer ticks.
  fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: utterance } })
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

const expectRecognizedButNotRunnable = async (
  fetchMock: FetchMock,
  blockedLabel: '暂未准备好' | '等待连接恢复' | '能力说明暂不可用',
) => {
  expect(await screen.findByText(/^查看(?:并修改|策略)$/)).toBeInTheDocument()
  expect(screen.getAllByText('MACD 金叉').length).toBeGreaterThan(0)
  expect(screen.getAllByText('MACD 死叉').length).toBeGreaterThan(0)
  expect(document.querySelector('.app')).toHaveAttribute('data-api-mode', 'live')
  expect(screen.queryByText('界面预览')).not.toBeInTheDocument()
  expect(screen.queryByText(/固定样例/)).not.toBeInTheDocument()

  const start = await reviewControls().findByRole('button', { name: blockedLabel })
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

  it.each([false, true])('keeps pre-backtest turns visible when an edit is pending=%s', async (pending) => {
    let draftCalls = 0
    const original = '东方财富，MACD金叉买入，死叉卖出。'
    const edit = '卖出改成持有30个交易日，先不回测。'
    const acknowledgment = pending ? '还需核实持仓日期口径。' : '持有30个交易日卖出，尚未回测。'
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') return {
        ok: true, status: 200, json: async () => executionUnavailableCapabilities,
      }
      if (path === '/api/v1/strategy-drafts') {
        draftCalls += 1
        return { ok: true, status: 201, json: async () => draftCalls === 1
          ? { ...readyDraft, assistant_message: '原买卖规则已准备好。' }
          : { ...readyDraft, revision: 2, is_strategy_edit: true,
            status: pending ? 'needs_clarification' : 'ready',
            strategy: pending ? null : technicalStrategy,
            diagnostic_code: pending ? 'strategy_edit_clarification' : null,
            clarification: pending ? acknowledgment : null,
            assistant_message: acknowledgment } }
      }
      throw new Error(`unexpected Live request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    await renderLiveApp()
    await submitLiveRule(original)
    expect(await screen.findByText('原买卖规则已准备好。')).toBeVisible()
    await submitLiveRule(edit)
    expect(await screen.findByText(acknowledgment)).toBeVisible()
    expect(screen.getByText(original, { selector: 'p' })).toBeVisible()
    expect(screen.getByText('原买卖规则已准备好。')).toBeVisible()
    expect(screen.getByText(edit)).toBeVisible()
    expect(draftCalls).toBe(2)
    expect(fetchMock.mock.calls.some(([path]) => String(path) === '/api/v1/backtest-runs')).toBe(false)
  })

  it('rechecks failed metadata once after a ready draft without regenerating or running it', async () => {
    let online = false
    let capabilityCalls = 0
    const runnable = { ...executionUnavailableCapabilities, backtest_execution_available: true }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        capabilityCalls += 1
        if (!online) throw new TypeError('temporary connection failure')
        return { ok: true, status: 200, json: async () => runnable }
      }
      if (path === '/api/v1/strategy-drafts') {
        return { ok: true, status: 201, json: async () => ({ ...readyDraft, run_requested: true }) }
      }
      if (path === '/api/v1/backtest-runs/prepare') {
        return { ok: true, status: 200, json: async () => ({ ready: true }) }
      }
      throw new Error(`unexpected Live request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client } = await renderLiveApp()
    await waitFor(() => expect(client.getQueryState(['capabilities'])?.status).toBe('error'))
    expect(capabilityCalls).toBe(2)
    online = true
    await submitLiveRule('东方财富MACD金叉买入，死叉卖出')
    const review = within(await screen.findByRole('complementary', { name: '策略审阅' }))
    expect(await review.findByRole('button', { name: '开始回测' })).toBeEnabled()
    expect(client.getQueryState(['capabilities'])?.status).toBe('success')
    expect(capabilityCalls).toBe(4) // initial + retry, independent compile GET, recovery GET
    expect(fetchMock.mock.calls.filter(([path]) => String(path) === '/api/v1/strategy-drafts')).toHaveLength(1)
    expect(fetchMock.mock.calls.filter(([path]) => String(path) === '/api/v1/backtest-runs/prepare')).toHaveLength(1)
    expect(fetchMock.mock.calls.some(([path]) => String(path) === '/api/v1/backtest-runs')).toBe(false)
  })

  it('keeps cached metadata usable but still requires backend preparation and manual recovery', async () => {
    let online = true
    let preparationAllowed = true
    const prepareBodies: unknown[] = []
    const runnable = { ...executionUnavailableCapabilities, backtest_execution_available: true }
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        if (!online) throw new TypeError('metadata refresh failed')
        return { ok: true, status: 200, json: async () => runnable }
      }
      if (path === '/api/v1/strategy-drafts') return { ok: true, status: 201, json: async () => readyDraft }
      if (path === '/api/v1/backtest-runs/prepare') {
        prepareBodies.push(JSON.parse(String(init?.body)))
        return preparationAllowed
          ? { ok: true, status: 200, json: async () => ({ ready: true }) }
          : { ok: false, status: 503, json: async () => ({ error: {
            code: 'skill_history_unavailable', message: '历史数据尚未准备完成。',
          } }) }
      }
      throw new Error(`unexpected Live request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client } = await renderLiveApp()
    await submitLiveRule('东方财富MACD金叉买入，死叉卖出')
    expect(await reviewControls().findByRole('button', { name: '开始回测' })).toBeEnabled()
    online = false
    await act(async () => { await client.refetchQueries({ queryKey: ['capabilities'], exact: true }) })
    expect(client.getQueryState(['capabilities'])?.status).toBe('error')
    expect(client.getQueryData(['capabilities'])).toEqual(runnable)
    expect(reviewControls().getByRole('button', { name: '开始回测' })).toBeEnabled()

    const user = userEvent.setup()
    preparationAllowed = false
    await user.click(reviewControls().getByRole('button', { name: /区间/ }))
    fireEvent.change(screen.getByRole('spinbutton', { name: '初始资金' }), { target: { value: '500000' } })
    await user.click(screen.getByRole('button', { name: '完成' }))
    expect(await reviewControls().findByText('历史数据尚未准备完成。')).toBeVisible()
    expect(reviewControls().getByRole('button', { name: '暂时无法回测' })).toBeDisabled()
    expect(prepareBodies).toHaveLength(2)
    expect(prepareBodies[1]).toMatchObject({ strategy: { backtest: { initial_cash_cny: 500000 } } })
    online = true
    preparationAllowed = true
    await user.click(reviewControls().getByRole('button', { name: '重新检查数据' }))
    expect(await reviewControls().findByRole('button', { name: '开始回测' })).toBeEnabled()
    expect(prepareBodies).toHaveLength(3)
    expect(prepareBodies[2]).toEqual(prepareBodies[1])
    expect(fetchMock.mock.calls.filter(([path]) => String(path) === '/api/v1/strategy-drafts')).toHaveLength(1)
    expect(fetchMock.mock.calls.some(([path]) => String(path) === '/api/v1/backtest-runs')).toBe(false)
  }, 10_000) // Three preparations, metadata recovery and a parameter dialog; measured >5s in jsdom.

  it('ends unsuccessful automatic recovery and offers a manual metadata retry without losing the draft', async () => {
    let online = false
    let capabilityCalls = 0
    const runnable = { ...executionUnavailableCapabilities, backtest_execution_available: true }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        capabilityCalls += 1
        if (!online) throw new TypeError('temporary capability connection failure')
        return { ok: true, status: 200, json: async () => runnable }
      }
      if (path === '/api/v1/strategy-drafts') return { ok: true, status: 201, json: async () => readyDraft }
      if (path === '/api/v1/backtest-runs/prepare') return { ok: true, status: 200, json: async () => ({ ready: true }) }
      throw new Error(`unexpected Live request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    const { client } = await renderLiveApp()
    await waitFor(() => expect(client.getQueryState(['capabilities'])?.status).toBe('error'))
    await submitLiveRule('东方财富MACD金叉买入，死叉卖出')
    const review = reviewControls()
    await waitFor(() => expect(review.getByRole('button', { name: '重新检查连接' })).toBeEnabled())
    expect(capabilityCalls).toBe(5) // two bounded cycles plus the compile metadata GET
    expect(screen.getByText(/暂未取得回测准备状态，策略和参数已保留/)).toBeVisible()
    expect(screen.queryByText(/浏览器没有连上回测服务/)).not.toBeInTheDocument()
    expect(screen.getByText(/^查看(?:并修改|策略)$/)).toBeVisible()
    online = true
    await userEvent.setup().click(review.getByRole('button', { name: '重新检查连接' }))
    expect(await review.findByRole('button', { name: '开始回测' })).toBeEnabled()
    expect(capabilityCalls).toBe(6)
    expect(fetchMock.mock.calls.filter(([path]) => String(path) === '/api/v1/strategy-drafts')).toHaveLength(1)
    expect(fetchMock.mock.calls.some(([path]) => String(path) === '/api/v1/backtest-runs')).toBe(false)
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

    await expectRecognizedButNotRunnable(fetchMock, '暂未准备好')
    expect(screen.getAllByText(
      '系统暂未准备好执行这次回测，规则和参数已保留，目前不会提交运行。',
    ).length).toBeGreaterThan(0)
    expect(screen.queryByRole('region', { name: '策略能力状态' })).not.toBeInTheDocument()
  })

  it.each([
    { status: 401, detail: '能力查询未通过鉴权。', invalidJson: false },
    { status: 503, detail: '能力元数据暂不可用', invalidJson: false },
    { status: 200, detail: '请求链路返回内容异常，系统暂未取得可用结果。', invalidJson: true },
  ])('keeps the actual metadata failure instead of labeling HTTP $status as network loss', async ({ status, detail, invalidJson }) => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') {
        return {
          ok: status < 400,
          status,
          json: async () => {
            if (invalidJson) throw new SyntaxError('invalid metadata response')
            return {
              error: { code: 'capabilities_unavailable', message: detail },
              request_id: `request-capabilities-${status}`,
            }
          },
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

    await expectRecognizedButNotRunnable(fetchMock, '能力说明暂不可用')
    expect(screen.getAllByText(/暂未取得回测准备状态/).length).toBeGreaterThan(0)
    expect(reviewControls().getByRole('status')).toHaveTextContent(detail)
    if (status >= 400) expect(reviewControls().getByRole('status')).toHaveTextContent(`HTTP ${status}`)
    expect(reviewControls().getByRole('button', { name: '重新读取能力' })).toBeEnabled()
    expect(reviewControls().queryByRole('button', { name: '等待连接恢复' })).not.toBeInTheDocument()
    expect(reviewControls().queryByRole('button', { name: '重新检查连接' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: '策略能力状态' })).not.toBeInTheDocument()
  })

  it('explains the exact unavailable sell condition below the blocked action and preserves the draft', async () => {
    const utterance = '东方财富 MACD 刚金叉，而且股价也站上 20 日线了就买入；MACD 死叉就卖出，看看近 1 年效果'
    const dataGap = '当前回测区间缺少有效 MACD 历史指标值。'
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/v1/capabilities') return { ok: true, status: 200, json: async () => ({
        ...executionUnavailableCapabilities, backtest_execution_available: true,
      }) }
      if (path === '/api/v1/strategy-drafts') return { ok: true, status: 201, json: async () => readyDraft }
      if (path === '/api/v1/backtest-runs/prepare') return { ok: false, status: 422, json: async () => ({
        error: {
          code: 'skill_indicator_history_not_ready', message: '策略和参数已保留，本次未启动回测。',
          details: [{ location: '/exit/children/0', type: 'backtest_condition_unavailable', message: dataGap }],
        },
      }) }
      throw new Error(`unexpected Live request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    await renderLiveApp()
    await submitLiveRule(utterance)

    const start = await reviewControls().findByRole('button', { name: '暂时无法回测' })
    const explanation = reviewControls().getByRole('status')
    expect(start).toBeDisabled()
    expect(explanation).toHaveTextContent(`卖出条件「MACD 死叉」：${dataGap}`)
    expect(explanation).toHaveTextContent('策略和参数已保留，本次未启动回测。')
    expect(start.compareDocumentPosition(explanation) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(screen.getByText(utterance)).toBeVisible()
    expect(screen.getByText(/^查看(?:并修改|策略)$/)).toBeVisible()
    expect(screen.getAllByText('MACD 金叉').length).toBeGreaterThan(0)
    expect(screen.getAllByText('MACD 死叉').length).toBeGreaterThan(0)
    await userEvent.setup().click(start)
    expectLiveCompileRequest(fetchMock)
    expect(fetchMock.mock.calls.filter(([path]) => String(path) === '/api/v1/backtest-runs/prepare')).toHaveLength(1)
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
      if (path === '/api/v1/backtest-runs/prepare') {
        return { ok: true, status: 200, json: async () => ({ ready: true }) }
      }
      throw new Error(`unexpected Live request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    await renderLiveApp()
    await submitLiveRule('东方财富季度报告发布后买入，收益33%止盈或高点回撤3%卖出')

    expect(await screen.findByText(/^查看(?:并修改|策略)$/)).toBeInTheDocument()
    expect(screen.getAllByText('季度报告发布').length).toBeGreaterThan(0)
    expect(screen.getAllByText('持仓收益达到 33% 止盈').length).toBeGreaterThan(0)
    expect(screen.getAllByText('持仓后收盘高点回撤 3% 卖出').length).toBeGreaterThan(0)
    expect(screen.queryByText(/只开放年度报告/)).not.toBeInTheDocument()
    const review = reviewControls()
    await waitFor(() => expect(review.getByRole('button', { name: '开始回测' })).toBeEnabled())
    expect(screen.queryByRole('region', { name: '策略能力状态' })).not.toBeInTheDocument()

    await userEvent.setup().click(screen.getByRole('button', { name: /持仓收益达到 33% 止盈/ }))
    expect(screen.getByRole('combobox', { name: '卖出条件关系' })).toBeEnabled()
    expect(screen.getByRole('spinbutton', { name: '幅度（%）' })).toHaveValue(33)
  })
})
