import { describe, expect, it } from 'vitest'

import type {
  BacktestActivity,
  BacktestSignalEvidence,
  CapabilitiesResponse,
  StrategyDraft,
  StrategyParameter,
  StrategySpecCondition,
  StrategySpecIndicatorCondition,
} from './shared/api/types'
import type { BacktestMetrics, RunEvidence } from './types'
import { DEFAULT_EXECUTION_SETTINGS } from './shared/config/backtest'
import { gridReviewPresentation } from './shared/price-plan'
import {
  assessStrategyCapabilities,
  conciseStrategyTitle,
  backtestPreparationFailureReason,
  buildChain,
  entryTriggerSemantics,
  secondaryMetric,
  strategyRuleTrees,
  summarizeExecution,
  summarizeRule,
  toChartMarks,
  toOrderRows,
  toBacktestMetrics,
  toSeries,
  toStrategySummary,
  toTradeRows,
} from './view-model'

it('审阅摘要保留核心条件值但把技术参数留给二级编辑', () => {
  const candidate: StrategyDraft = {
    ...draft,
    entry: {
      operator: 'all',
      conditions: [{
        id: 'entry-rsi', kind: 'indicator', indicatorId: 'technical.rsi',
        label: '相对强弱指标 RSI 由下向上穿过阈值 30', trigger: '由下向上穿过阈值',
        timeframe: '1d', evaluationMode: 'bar_close_confirmed',
        parameters: [
          { key: 'period', label: '周期', value: 14, min: 1, max: 500 },
          { key: 'threshold', label: '阈值', value: 30, min: 0, max: 100 },
        ],
      }],
    },
    exit: {
      operator: 'first_of',
      conditions: [{
        id: 'exit-holding', kind: 'holding_period', label: '成交后第 10 个交易日尝试卖出',
        trigger: '持有期退出', sessions: 10, anchor: 'first_entry_fill',
        countMode: 'subsequent_trading_sessions', execution: 'target_session_open_proxy',
      }],
    },
  }
  const trees = strategyRuleTrees(candidate)
  expect(summarizeRule(trees.entry)).toBe('相对强弱指标 RSI 由下向上穿过阈值 30')
  expect(summarizeRule(trees.exit)).toBe('成交后第 10 个交易日尝试卖出')
  expect(JSON.stringify(trees.entry)).toContain('阈值 30')
  expect(JSON.stringify(trees.entry)).toContain('周期 14')
  expect(JSON.stringify(trees.exit)).toContain('成交后第 10 个交易日尝试卖出')
})

it.each(['entry', 'exit'] as const)('网格组合独立%s不再混入该侧网格条件', (side) => {
  const spec = { ...draft.strategySpec, trading_plan: { kind: 'grid' as const, parameters: {
    anchor_mode: 'first_open', spacing_mode: 'cny', spacing: 1, order_shares: 100,
    min_shares: 0, max_shares: 10000,
  } }, entry: side === 'entry' ? draft.strategySpec.entry : null,
  exit: side === 'exit' ? draft.strategySpec.exit : null }
  const composed = { ...draft, strategySpec: spec }
  const before = JSON.stringify(composed)
  const trees = strategyRuleTrees(composed)
  expect(JSON.stringify(trees[side])).toContain(side === 'entry' ? '年度报告发布' : 'MACD')
  expect(JSON.stringify(trees[side])).not.toContain('跨一格')
  expect(JSON.stringify(trees[side === 'entry' ? 'exit' : 'entry'])).toContain('跨一格')
  const review = gridReviewPresentation(spec.trading_plan, spec)
  expect(review[side === 'entry' ? 'buy' : 'sell']).toEqual([])
  expect(JSON.stringify(composed)).toBe(before)
})

it('组合标题保留每种指标，不把或关系写成且，也不修改策略', () => {
  for (const operator of ['all', 'any'] as const) {
    const combined: StrategyDraft = { ...draft, entry: { operator, conditions: [
      { id: 'kdj', kind: 'indicator', indicatorId: 'technical.kdj', label: 'KDJ 金叉', trigger: '金叉', timeframe: '1d', evaluationMode: 'bar_close_confirmed', parameters: [] },
      { id: 'rsi', kind: 'indicator', indicatorId: 'technical.rsi', label: 'RSI 低于 50', trigger: '低于', timeframe: '1d', evaluationMode: 'bar_close_confirmed', parameters: [] },
    ] } }
    const before = JSON.stringify(combined)
    expect(conciseStrategyTitle(combined)).toBe('KDJ 金叉且RSI 低于 50时买入，MACD 死叉卖出')
    expect(JSON.stringify(combined)).toBe(before)
  }
})

it('动态指标组合保留可读名称，不降级成其他条件', () => {
  const combined: StrategyDraft = { ...draft, entry: { operator: 'all', conditions: [
    { id: 'macd', kind: 'indicator', indicatorId: 'technical.macd', label: 'MACD 金叉', trigger: '金叉', timeframe: '1d', evaluationMode: 'bar_close_confirmed', parameters: [] },
    { id: 'flow', kind: 'indicator', indicatorId: 'data.numeric', label: '主力资金净流入额高于0元', trigger: '高于', timeframe: '1d', evaluationMode: 'bar_close_confirmed', parameters: [] },
  ] } }
  expect(conciseStrategyTitle(combined)).toBe('MACD 金叉且主力资金净流入额高于0元时买入，MACD 死叉卖出')
})

it('把趋势回踩的买卖条件整合成一句交易意图', () => {
  const indicator = (id: string, indicatorId: string, label: string, parameters: StrategyParameter[]) => ({ id, kind: 'indicator' as const, indicatorId, label, trigger: label,
    timeframe: '1d' as const, evaluationMode: 'bar_close_confirmed' as const, parameters })
  const combined: StrategyDraft = { ...draft,
    entry: { operator: 'all', conditions: [
      indicator('trend', 'technical.ma_cross', '20 日均线高于 60 日均线', []),
      indicator('pullback', 'price.return_pct', '当日涨幅 < 0%', []),
      indicator('support', 'technical.ma', '价格高于 20 日均线', [{ key: 'period', label: '周期', value: 20, min: 1, max: 500 }]),
    ] },
    exit: { operator: 'first_of', conditions: [
      indicator('break', 'technical.ma', '价格下穿 20 日均线', [{ key: 'period', label: '周期', value: 20, min: 1, max: 500 }]),
      { id: 'holding', kind: 'holding_period', label: '成交后第 10 个交易日尝试卖出', trigger: '持有期退出', sessions: 10,
        anchor: 'first_entry_fill', countMode: 'subsequent_trading_sessions', execution: 'target_session_open_proxy' },
    ] },
  }
  expect(conciseStrategyTitle(combined)).toBe('上涨趋势中回踩买入，跌破 20 日线或持有 10 个交易日卖出')
})

const draft: StrategyDraft = {
  id: 'draft-event',
  revision: 2,
  strategyHash: `sha256:${'d'.repeat(64)}`,
  sourceText: '东方财富年度报告发布后买入，MACD 死叉卖出',
  title: '年报 + MACD 规则 · 日线',
  instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' },
  confidence: 1,
  entry: {
    operator: 'all',
    conditions: [{
      id: 'entry-event', kind: 'event', eventCode: 'event.financial_results.annual_report',
      label: '年度报告发布', trigger: '按首次可获得时间确认', attributes: {},
    }],
  },
  exit: {
    operator: 'first_of',
    conditions: [{
      id: 'exit-macd', kind: 'indicator', indicatorId: 'technical.macd', label: 'MACD 死叉',
      trigger: '死叉', timeframe: '1d', evaluationMode: 'bar_close_confirmed', parameters: [],
    }],
  },
  execution: {
    entryPolicy: 'next_tradable_session_open', exitPolicy: 'next_tradable_session_open',
    priceLimitMode: 'wait_for_unlock', tPlusOne: true, dataCapability: 'daily_ohlcv_events',
    evaluationFrequency: 'event_available_plus_1d_close', capacityMode: 'point_in_time_volume',
    participationRate: 0.05, allocationRatio: 1, commissionRate: 0.0003,
    minimumCommissionCny: 5, slippageBps: 5, retryUnfilledExits: true,
    maxExitAttempts: 20, warmupCalendarDays: 120, settlementExtensionDays: 30,
    runRobustness: false,
  },
  backtest: { start: '2024-01-01', end: '2024-12-31', initialCashCny: 1_000_000 },
  assumptions: [], warnings: [],
  strategySpec: {
    schema_version: 'strategy.v1',
    catalog: { catalog_id: 'cn_a.signals', release_version: 'v2' },
    instrument: { market: 'CN_A', symbol: '300059.SZ', position_mode: 'long_only' },
    entry: {
      type: 'event_condition', event_code: 'event.financial_results.annual_report',
      definition_version: '1.0.0', trigger: 'published', attributes: {},
    },
    exit: { op: 'first_of', children: [{
      type: 'indicator_condition', indicator_id: 'technical.macd', definition_version: '1.0.0',
      params: { fast: 12, slow: 26, signal: 9 }, timeframe: '1d',
      evaluation_mode: 'bar_close_confirmed', trigger: 'death_cross', value: null,
    }] },
    execution: {
      timezone: 'Asia/Shanghai', entry_policy: 'next_tradable_session_open',
      exit_policy: 'next_tradable_session_open', data_capability: 'daily_ohlcv_events',
      execution_resolution: '1d', evaluation_frequency: 'event_available_plus_1d_close',
      position_policy: 'single_position_no_pyramiding', t_plus_one: true,
    },
    backtest: { start: '2024-01-01', end: '2024-12-31', initial_cash_cny: 1_000_000 },
  },
}

describe('entry trigger annotation', () => {
  const indicator = (trigger: string): StrategySpecIndicatorCondition => ({
    type: 'indicator_condition', indicator_id: 'technical.rsi', definition_version: '1.0.0',
    params: { period: 14 }, timeframe: '1d', evaluation_mode: 'bar_close_confirmed',
    trigger, value: 30,
  })
  const withEntry = (entry: StrategySpecCondition): StrategyDraft => ({
    ...draft,
    entry: { operator: 'all', conditions: [] },
    strategySpec: {
      ...draft.strategySpec, entry,
      execution: { ...draft.strategySpec.execution, position_policy: 'accumulate_on_new_entry_signal' },
    },
  })

  it.each([
    ['crosses_above', '上穿'],
    ['crosses_below', '下穿'],
    ['golden_cross', '金叉'],
    ['death_cross', '死叉'],
  ])('classifies root %s from the final spec', (trigger, direction) => {
    const entry = indicator(trigger)
    if (trigger === 'golden_cross' || trigger === 'death_cross') {
      entry.indicator_id = 'technical.macd'
      entry.params = { fast: 12, slow: 26, signal: 9 }
      entry.value = null
    }
    expect(toStrategySummary(withEntry(entry)).entryTriggerNote).toMatch(new RegExp(`^${direction}触发`))
  })

  it('explains first-event triggering and a later new signal separately', () => {
    const candidate = withEntry(indicator('crosses_above'))
    expect(entryTriggerSemantics(candidate)).toEqual({
      trigger: '上穿发生时触发；持续在阈值上方不重复触发',
      repeat: '持仓期间出现新的买入触发，可以再次买入',
    })
  })

  it('describes states and composite roots as newly satisfied, never as a child crossing', () => {
    const financial: StrategySpecCondition = {
      type: 'financial_condition', metric_id: 'financial.roe', definition_version: '1.0.0',
      report_type: 'annual', period_basis: 'full_year', statement_scope: 'consolidated',
      revision_policy: 'as_known_at_signal', comparator: 'gt', value: 15, unit: 'PERCENT',
    }
    const entries: StrategySpecCondition[] = [
      indicator('above'), indicator('below'), financial,
      { type: 'all', children: [indicator('crosses_above'), financial] },
      { type: 'any', children: [indicator('death_cross'), indicator('below')] },
      { type: 'not', child: indicator('crosses_above') },
    ]
    for (const entry of entries) {
      const candidate = withEntry(entry)
      const before = JSON.stringify(candidate)
      expect(toStrategySummary(candidate).entryTriggerNote, JSON.stringify(entry))
        .toBe('新满足时买入，持续满足不重复买入')
      expect(JSON.stringify(candidate)).toBe(before)
    }
  })

  it('omits the annotation for legacy policies, root events and every trading-plan kind', () => {
    for (const policy of ['single_position_no_pyramiding', 'bounded_inventory'] as const) {
      const candidate = withEntry(indicator('crosses_above'))
      candidate.strategySpec.execution.position_policy = policy
      expect(toStrategySummary(candidate).entryTriggerNote, policy).toBeUndefined()
    }
    expect(toStrategySummary(withEntry(draft.strategySpec.entry!)).entryTriggerNote).toBeUndefined()
    for (const kind of ['grid', 'conditional', 'scheduled'] as const) {
      const candidate = withEntry(indicator('crosses_above'))
      candidate.strategySpec.trading_plan = { kind, parameters: {
        anchor_mode: 'first_open', spacing_mode: 'cny', spacing: 1, order_shares: 100,
        min_shares: 0, max_shares: 10000, rules: [],
      } }
      expect(toStrategySummary(candidate).entryTriggerNote, kind).toBeUndefined()
    }
  })
})

const evidence: BacktestSignalEvidence = {
  type: 'event_observation',
  id: 'AN202403141626765931',
  availableAt: '2024-03-14T20:57:33+08:00',
  sourceEventId: 'AN202403141626765931',
  provider: 'eastmoney',
  sourceUrl: 'https://data.eastmoney.com/notices/detail/300059/AN202403141626765931.html',
  timeQuality: 'vendor_observed',
  timestampPrecision: 'second',
  validationStatus: 'validated',
  rawResponseSha256: '5afd37736349f7adcf8104dc4e0c9e33339e680e371cef9dd2be2bb38cebcb43',
}

const activities: BacktestActivity[] = [
  {
    id: 'signal:decision-1', kind: 'signal', occurredAt: '2024-03-14T20:57:33+08:00',
    side: 'buy', title: '年度报告首次可得', status: 'confirmed',
    reason: '年报已达到可使用时点。', chainId: 'decision-1', decisionId: 'decision-1',
    signalSemantics: 'event', signalValiditySessions: 1,
    originSignalId: 'signal:decision-1', outcomeReason: 'matched_at_open', evidence: [evidence],
  },
  {
    id: 'order:order-1', kind: 'order', occurredAt: '2024-03-15T09:15:00+08:00',
    side: 'buy', title: '提交买入委托', status: 'submitted', reason: '下一可交易日提交。',
    chainId: 'decision-1', decisionId: 'decision-1', orderId: 'order-1',
    parentId: 'signal:decision-1', price: 13.82, quantity: 3_800,
    signalSemantics: 'event', signalValiditySessions: 1, attemptNo: 1,
    originSignalId: 'signal:decision-1', outcomeReason: 'matched_at_open',
  },
  {
    id: 'fill:fill-1', kind: 'fill', occurredAt: '2024-03-15T09:30:00+08:00',
    side: 'buy', title: '买入成交', status: 'filled', reason: '使用日线开盘价代理。',
    chainId: 'decision-1', decisionId: 'decision-1', orderId: 'order-1', fillId: 'fill-1',
    parentId: 'order:order-1', price: 13.82, quantity: 3_800,
    signalSemantics: 'event', signalValiditySessions: 1, attemptNo: 1,
    originSignalId: 'signal:decision-1', outcomeReason: 'matched_at_open',
    capacityReasonCode: 'capacity_previous_session_volume_proxy',
    timeQuality: 'daily_bar_open_proxy',
    timeSemantics: '日线 OHLCV 的开盘价代理记录边界，不代表真实逐笔成交',
  },
]

const metrics: BacktestMetrics = {
  total: -2.5, bench: -39, excess: 36.5, benchmarkComparisonStatus: 'comparable', mdd: -12, trips: 1, win: null,
  sharpe: null, ann: null, initialCashCny: 1_000_000, finalEquityCny: 975_000,
  interpretation: '样本不足', dataRange: { start: '2024-01-01', end: '2024-12-31', sessions: 2 },
  warnings: [], openShares: null,
}

const runEvidence: RunEvidence = {
  runId: 'run:1', gitSha: 'a'.repeat(40), snapshotId: 'composite:v2',
  snapshotChecksum: `sha256:${'b'.repeat(64)}`,
  dataSchemaVersion: 'local-parquet.market-data.v3',
  producerSnapshotSchemaVersion: 'ashare-lab.composite-research-snapshot.v2',
  producerSnapshotId: `composite:${'e'.repeat(64)}`,
  catalog: `sha256:${'c'.repeat(64)}`, strategyHash: `sha256:${'d'.repeat(64)}`,
  engineVersion: '0.3.0', executionAssumptions: {},
}

describe('result view model', () => {
  it('keeps hybrid timing distinct from daily-only and event confirmation in cards and traces', () => {
    const hybrid = structuredClone(draft)
    hybrid.execution.evaluationFrequency = 'daily_close_and_minute_bar'
    const summary = toStrategySummary(hybrid)
    expect(summary.confirmation).toContain('分钟保护独立触发')
    expect(summary.earliestExecution).toContain('下一市场交易日开盘')
    const chain = buildChain(hybrid, activities, toTradeRows(activities)[0]!)
    expect(chain.some(node => node.detail.includes('确认：日线信号收盘确认，分钟保护独立触发'))).toBe(true)
    expect(chain.some(node => node.detail.includes('确认：事件首次可得'))).toBe(false)
  })

  it('computes return difference in percentage points', () => {
    const result = toBacktestMetrics({
      runId: 'run:relative', totalReturn: -0.1049, benchmarkReturn: -0.2694,
      benchmarkComparisonStatus: 'comparable', annualizedReturn: null,
      maxDrawdown: -0.1, sharpeRatio: null, winRate: null, tradeCount: 1,
      initialCashCny: 1000000, finalEquityCny: 895100, interpretation: 'test', warnings: [],
      dataRange: { start: '2025-01-01', end: '2025-12-31', sessions: 240 },
    })
    expect(result.excess).toBeCloseTo(16.45, 4)
  })

  it('identifies the original failing sell rule after a position-aware exit', () => {
    const mixed = structuredClone(draft)
    mixed.exit.conditions.unshift({
      id: 'holding', kind: 'holding_period', label: '持有 30 日', trigger: '到期卖出',
      sessions: 30, anchor: 'first_entry_fill', countMode: 'subsequent_trading_sessions',
      execution: 'target_session_open_proxy',
    })
    mixed.strategySpec.exit!.children.unshift({ type: 'holding_period_exit', sessions: 30,
      anchor: 'first_entry_fill', count_mode: 'subsequent_trading_sessions', execution: 'target_session_open_proxy' })
    const reason = backtestPreparationFailureReason(mixed, {
      type: 'about:blank', title: '请求未完成', status: 422,
      detail: '请延长区间；本次未启动回测。',
      details: [{ location: '/exit/children/1', type: 'backtest_condition_unavailable',
        message: '当前区间没有有效历史指标值。' }],
    })
    expect(reason).toBe('卖出条件「MACD 死叉」：当前区间没有有效历史指标值。\n请延长区间；本次未启动回测。')
    expect(reason).not.toContain('持有 30 日')
  })

  it('preserves exact data gaps but never guesses a rule from an unknown source pointer', () => {
    const reason = backtestPreparationFailureReason(draft, {
      type: 'about:blank', title: '请求未完成', status: 503, detail: '请稍后重新读取。',
      details: [
        { location: 'market_history', type: 'backtest_data_missing', message: '取数区间缺少跌停价。' },
        { location: '/exit/children/99', type: 'backtest_condition_unavailable', message: '未定位到有效历史指标值。' },
        { location: '/entry', type: 'unknown_internal_type', message: '不要显示内部诊断。' },
      ],
    })
    expect(reason).toBe('取数区间缺少跌停价。\n未定位到有效历史指标值。\n请稍后重新读取。')
    expect(reason).not.toMatch(/MACD|年度报告|内部诊断/)
    expect(backtestPreparationFailureReason(draft, {
      type: 'about:blank', title: '网络错误', status: 0, detail: '连接暂时中断。',
    })).toBe('连接暂时中断。')
  })
  it('labels system-default execution settings without counting strategy-owned policies', () => {
    expect(summarizeExecution({ ...draft, execution: {
      ...draft.execution, ...DEFAULT_EXECUTION_SETTINGS,
    } })).toBe('默认')
  })

  it('keeps adjusted fees labelled after the draft is saved or the stock changes', () => {
    const execution = { ...draft.execution, ...DEFAULT_EXECUTION_SETTINGS,
      slippageBps: 2, commissionRate: 0, minimumCommissionCny: 0 }
    expect(summarizeExecution({ ...draft, execution })).toBe('已调整 3 项')
    expect(summarizeExecution({ ...draft, id: 'rebound-draft', revision: 8,
      instrument: { ...draft.instrument, symbol: '601995.SH', name: '中金公司', exchange: 'SSE' },
      execution: { ...execution },
    })).toBe('已调整 3 项')
  })

  it('returns to the default label only after the settings return to system defaults', () => {
    const execution = { ...draft.execution, ...DEFAULT_EXECUTION_SETTINGS, slippageBps: 2 }
    expect(summarizeExecution({ ...draft, execution })).toBe('已调整 1 项')
    expect(summarizeExecution({ ...draft, execution: {
      ...execution, slippageBps: DEFAULT_EXECUTION_SETTINGS.slippageBps,
    } })).toBe('默认')
  })

  it('counts fixed CNY slippage independently and excludes an explicit zero', () => {
    const execution = { ...draft.execution, ...DEFAULT_EXECUTION_SETTINGS, slippageCny: 0.02 }
    expect(summarizeExecution({ ...draft, execution })).toBe('已调整 1 项')
    expect(summarizeExecution({ ...draft, execution: { ...execution, slippageBps: 0 } }))
      .toBe('已调整 2 项')
    expect(summarizeExecution({ ...draft, execution: { ...execution, slippageCny: 0 } }))
      .toBe('默认')
  })

  it('preserves minute signal timestamps supplied inside execution details', () => {
    const signalAt = '2026-09-09T09:31:00+08:00'
    const events: BacktestActivity[] = [
      { ...activities[1]!, id: 'minute-order', kind: 'order', orderId: 'minute-1',
        occurredAt: signalAt, executionDetails: { signalAt } },
      { ...activities[2]!, id: 'minute-fill', kind: 'fill', orderId: 'minute-1',
        occurredAt: '2026-09-09T09:32:00+08:00', executionDetails: { signalAt } },
    ]
    const rows = toOrderRows(toTradeRows(events))
    expect(rows).toHaveLength(1)
    expect(rows[0]?.signalAt).toBe(signalAt)
    expect(rows[0]?.filledAt).toBe('2026-09-09T09:32:00+08:00')
    const minuteDraft = { ...draft, execution: { ...draft.execution, evaluationFrequency: '1m_bar' as const } }
    const text = JSON.stringify(buildChain(minuteDraft, events, toTradeRows(events)[1]!))
    expect(text).toContain('分钟行情；新触发委托下一根生效')
    expect(text).not.toContain('事件首次可得')
  })

  it('keeps retry orders in one signal chain as separate broker-style rows', () => {
    const retryActivities: BacktestActivity[] = [
      activities[0]!,
      {
        ...activities[1]!, id: 'order:order-retry-1', orderId: 'order-retry-1',
        occurredAt: '2024-03-15T09:15:00+08:00', attemptNo: 1,
      },
      {
        ...activities[2]!, id: 'no-fill:retry-1', kind: 'unfilled', orderId: 'order-retry-1',
        parentId: 'order:order-retry-1', occurredAt: '2024-03-15T09:30:00+08:00',
        status: 'cancelled', title: '买入未成交', reason: '开盘一字涨停。', price: null,
        quantity: 0, outcomeReason: 'blocked_by_price_limit', attemptNo: 1,
      },
      {
        ...activities[1]!, id: 'order:order-retry-2', orderId: 'order-retry-2',
        occurredAt: '2024-03-18T09:15:00+08:00', price: 14.05, attemptNo: 2,
      },
      {
        ...activities[2]!, id: 'fill:retry-2', orderId: 'order-retry-2',
        parentId: 'order:order-retry-2', occurredAt: '2024-03-18T09:30:00+08:00',
        price: 14.05, attemptNo: 2,
      },
    ]

    const rows = toOrderRows(toTradeRows(retryActivities))

    expect(rows).toHaveLength(2)
    expect(rows.map((row) => row.status)).toEqual(['unfilled', 'filled'])
    expect(rows.map((row) => row.orderPrice)).toEqual([13.82, 14.05])
    expect(rows.every((row) => row.title === '年度报告首次可得')).toBe(true)
    expect(rows[0]?.activityIds).toContain('no-fill:retry-1')
    expect(rows[1]?.activityIds).toContain('fill:retry-2')
  })

  it('keeps a partially filled order partial when its remainder later expires', () => {
    const partialActivities: BacktestActivity[] = [
      activities[0]!,
      activities[1]!,
      {
        ...activities[2]!, id: 'partial:fill-1', kind: 'partial_fill', status: 'partially_filled',
        title: '买入部分成交', quantity: 1_900,
      },
      {
        ...activities[2]!, id: 'expired:order-1', kind: 'expired', status: 'expired',
        occurredAt: '2024-03-15T15:00:00+08:00', title: '剩余委托过期', quantity: 1_900,
        outcomeReason: 'day_order_expired', price: null,
      },
    ]

    const rows = toOrderRows(toTradeRows(partialActivities))

    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ status: 'partial', price: 13.82, quantity: 1_900 })
    expect(rows[0]?.activityIds).toEqual(expect.arrayContaining([
      'partial:fill-1', 'expired:order-1',
    ]))
  })

  it('does not turn a signal without an order into a broker order row', () => {
    expect(toOrderRows(toTradeRows([activities[0]!]))).toEqual([])
  })

  it('keeps the original submission time when an order has later working updates', () => {
    const events: BacktestActivity[] = [activities[0]!, activities[1]!,
      { ...activities[1]!, id: 'working-update', occurredAt: '2024-03-15T14:59:00+08:00' },
      { ...activities[2]!, id: 'partial-late', kind: 'partial_fill', occurredAt: '2024-03-15T14:58:00+08:00' },
    ]
    const rows = toOrderRows(toTradeRows(events))
    expect(rows).toHaveLength(1)
    expect(rows[0]?.orderAt).toBe(activities[1]!.occurredAt)
    expect(rows[0]?.filledAt).toBe('2024-03-15T14:58:00+08:00')
  })

  it('keeps scheduled fill quantity separate from the cancelled remainder', () => {
    const scheduled: BacktestActivity[] = [
      { ...activities[1]!, quantity: null },
      { ...activities[2]!, id: 'scheduled:partial', kind: 'partial_fill',
        status: 'partially_filled', quantity: 57 },
      { ...activities[2]!, id: 'scheduled:cancel', kind: 'expired', status: 'cancelled',
        occurredAt: '2024-03-15T15:00:00+08:00', quantity: 43, price: null,
        outcomeReason: 'day_order_expired', executionDetails: {
          requestedQuantity: 100, filledQuantity: 57, cancelledQuantity: 43,
        } },
    ]
    const rows = toOrderRows(toTradeRows(scheduled))
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ status: 'partial', quantity: 57 })
    expect(rows[0]?.activityIds).toContain('scheduled:cancel')
  })

  it.each([
    ['exit_retry_budget_exhausted', '已达到卖出尝试上限'],
    ['exit_retry_disabled', '高级设置关闭了卖出重试'],
  ])('explains %s without inventing another order or losing a partial fill', (reason, explanation) => {
    const signal = { ...activities[0]!, side: 'sell' as const }
    const order = { ...activities[1]!, side: 'sell' as const }
    const partial: BacktestActivity = {
      ...activities[2]!, side: 'sell', id: 'partial:sell', kind: 'partial_fill',
      status: 'partially_filled', title: '卖出部分成交', quantity: 1900,
    }
    const stop: BacktestActivity = {
      ...partial, id: 'stop:sell', kind: 'unfilled', status: 'expired',
      title: '卖出尝试已结束', reason, outcomeReason: reason,
      occurredAt: '2024-03-15T15:00:00+08:00',
      price: null, quantity: null, notionalCny: null, fillId: undefined,
    }
    const events = [signal, order, partial, stop]
    const rows = toOrderRows(toTradeRows(events))
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({ status: 'partial', quantity: 1900 })
    expect(rows[0]?.activityIds).toContain(stop.id)
    const chain = buildChain(draft, events, toTradeRows(events).find((row) => row.id === stop.id)!)
    const terminal = chain.find((node) => node.title === stop.title)
    expect(terminal?.detail).toContain(explanation)
    expect(terminal?.detail).not.toContain(reason)
    expect(terminal?.detail).toContain('可调整')
    expect(terminal?.facts?.some((fact) => fact.label === '本次结果')).toBe(false)
    expect(terminal?.technicalFacts).toContainEqual({ label: '本次结果代码', value: reason })
  })

  it('chooses the most useful available secondary result metric', () => {
    expect(secondaryMetric({ ...metrics, win: 56.71, ann: 8.2 })).toMatchObject({ label: '胜率' })
    expect(secondaryMetric({ ...metrics, win: null, ann: 8.2 })).toMatchObject({ label: '年化收益' })
    expect(secondaryMetric({ ...metrics, win: null, ann: null })).toMatchObject({ label: '完整买卖' })
  })

  it('keeps nested AND, OR, and NOT relationships instead of flattening them into plus signs', () => {
    const compound: StrategyDraft = {
      ...draft,
      entry: {
        operator: 'all',
        conditions: [
          draft.entry.conditions[0]!,
          {
            id: 'entry-rsi', kind: 'indicator', indicatorId: 'technical.rsi',
            label: 'RSI 低于 30', trigger: 'RSI(14) 低于 30', timeframe: '1d',
            evaluationMode: 'bar_close_confirmed', parameters: [],
          },
          {
            id: 'entry-ma', kind: 'indicator', indicatorId: 'technical.ma',
            label: '收盘高于 20 日均线', trigger: '收盘价高于均线', timeframe: '1d',
            evaluationMode: 'bar_close_confirmed', parameters: [],
          },
        ],
      },
      strategySpec: {
        ...draft.strategySpec,
        entry: {
          type: 'all',
          children: [
            draft.strategySpec.entry!,
            {
              type: 'any',
              children: [
                {
                  type: 'indicator_condition', indicator_id: 'technical.rsi',
                  definition_version: '1.0.0', params: { period: 14 }, timeframe: '1d',
                  evaluation_mode: 'bar_close_confirmed', trigger: 'below', value: 30,
                },
                {
                  type: 'not',
                  child: {
                    type: 'indicator_condition', indicator_id: 'technical.ma',
                    definition_version: '1.0.0', params: { period: 20 }, timeframe: '1d',
                    evaluation_mode: 'bar_close_confirmed', trigger: 'below', value: null,
                  },
                },
              ],
            },
          ],
        },
      },
    }

    const rules = strategyRuleTrees(compound)
    expect(rules.entry).toMatchObject({
      kind: 'group', operator: 'all', children: [
        { kind: 'leaf', label: '年度报告发布' },
        { kind: 'group', operator: 'any', children: [
          { kind: 'leaf', label: 'RSI 低于 30' },
          { kind: 'group', operator: 'not', children: [{ kind: 'leaf', label: '收盘高于 20 日均线' }] },
        ] },
      ],
    })
    expect(summarizeRule(rules.entry)).toBe(
      '（年度报告发布 且 （RSI 低于 30 或 非（收盘高于 20 日均线）））',
    )
    const card = toStrategySummary(compound)
    expect(card.rows).toEqual(expect.arrayContaining([
      expect.objectContaining({
        label: '买入',
        value: '年度报告发布 且 （RSI 低于 30 或 非（收盘高于 20 日均线））',
      }),
      expect.objectContaining({ label: '区间', value: '近一年' }),
    ]))
  })

  it('separates a compiled StrategySpec from preparation and pinned-snapshot availability', () => {
    const unavailableCapabilities: CapabilitiesResponse = {
      markets: ['CN_A'], input_modes: ['natural_language_zh'],
      strategy_scopes: ['single_instrument', 'long_only'], indicators: [{
        indicator_id: 'technical.macd', definition_version: '1.0.0', status: 'stable',
        display_name: 'MACD', description: 'MACD 指标', warmup_bars: 35,
        timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
        triggers: ['golden_cross', 'death_cross'],
        parameters: [], trigger_definitions: [],
      }],
      events: [{
        event_code: 'event.financial_results.annual_report', definition_version: '1.0.0',
        catalog_status: 'stable', status: 'unavailable', backtest_available: false,
        preparation_available: false, availability_scope: 'unavailable',
        unavailable_reason: 'snapshot_coverage_unavailable', triggers: ['published'],
      }],
      execution_policies: ['next_tradable_session_open'], event_catalog_status: 'published',
      event_backtest_available: false, event_preparation_available: false,
      event_availability_scope: 'unavailable', backtest_execution_available: true,
      limits: { max_body_bytes: 16_384, max_utterance_characters: 2000,
        max_instrument_context_characters: 32 },
    }

    const status = assessStrategyCapabilities(draft, unavailableCapabilities, 'live')
    expect(status.stages).toEqual(expect.arrayContaining([
      expect.objectContaining({ key: 'understand', state: 'available' }),
      expect.objectContaining({ key: 'prepare', state: 'unavailable' }),
      expect.objectContaining({ key: 'snapshot', state: 'unavailable' }),
    ]))
    expect(status.canRun).toBe(false)
    expect(status.reason).toContain('买入条件「年度报告发布」')
    expect(status.reason).toContain('公告历史数据')
    expect(status.reason).not.toContain('卖出条件')
    expect(status.events[0]).toMatchObject({
      catalog: 'available', preparable: 'unavailable', pinnedSnapshot: 'unavailable',
    })
  })

  it('uses the independent document-text capability for a report word-count strategy', () => {
    const documentTextDraft: StrategyDraft = {
      ...draft,
      sourceText: '东方财富年报正文中 AI 超过 5 次就买入，3 个交易日后卖出',
      entry: {
        operator: 'all',
        conditions: [{
          id: 'entry-event-text', kind: 'event',
          eventCode: 'event.financial_results.annual_report',
          label: '年度报告正文中“AI”完整词出现 > 5 次',
          trigger: '按首次可获得时间确认', attributes: {},
          documentText: {
            metric_id: 'document.literal_mention_count', metric_version: '1.0.0',
            term: 'AI', normalization: 'nfkc', match_mode: 'ascii_token',
            case_sensitive: false, comparator: 'gt', value: 5,
          },
        }],
      },
      strategySpec: {
        ...draft.strategySpec,
        entry: {
          type: 'event_condition', event_code: 'event.financial_results.annual_report',
          definition_version: '1.0.0', trigger: 'published', attributes: {},
          document_text: {
            metric_id: 'document.literal_mention_count', metric_version: '1.0.0',
            term: 'AI', normalization: 'nfkc', match_mode: 'ascii_token',
            case_sensitive: false, comparator: 'gt', value: 5,
          },
        },
      },
    }
    const capabilities: CapabilitiesResponse = {
      markets: ['CN_A'], input_modes: ['natural_language_zh'],
      strategy_scopes: ['single_instrument', 'long_only'], indicators: [{
        indicator_id: 'technical.macd', definition_version: '1.0.0', status: 'stable',
        display_name: 'MACD', description: 'MACD 指标', warmup_bars: 35,
        timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
        triggers: ['death_cross'], parameters: [], trigger_definitions: [],
      }],
      events: [{
        event_code: 'event.financial_results.annual_report', definition_version: '1.0.0',
        catalog_status: 'stable', status: 'available', backtest_available: true,
        preparation_available: false, availability_scope: 'pinned_snapshot',
        unavailable_reason: null, triggers: ['published'],
        document_text: {
          catalog_available: true, backtest_available: false, preparation_available: false,
          availability_scope: 'unavailable', unavailable_reason: 'snapshot_coverage_unavailable',
        },
      }],
      execution_policies: ['next_tradable_session_open'], event_catalog_status: 'published',
      event_backtest_available: true, event_preparation_available: false,
      event_availability_scope: 'pinned_snapshot', backtest_execution_available: true,
      limits: { max_body_bytes: 16_384, max_utterance_characters: 2000,
        max_instrument_context_characters: 32 },
    }

    const unavailable = assessStrategyCapabilities(documentTextDraft, capabilities, 'live')
    expect(unavailable.canRun).toBe(false)
    expect(unavailable.reason).toMatch(/完整正文/)
    expect(unavailable.events[0]).toMatchObject({
      catalog: 'available', preparable: 'unavailable', pinnedSnapshot: 'unavailable',
    })

    const preparable = assessStrategyCapabilities(documentTextDraft, {
      ...capabilities,
      events: [{
        ...capabilities.events[0]!,
        document_text: {
          catalog_available: true, backtest_available: false, preparation_available: true,
          availability_scope: 'request_preparation', unavailable_reason: 'preparation_required',
        },
      }],
    }, 'live')
    expect(preparable).toMatchObject({ canRun: true, needsPreparation: true })
    expect(preparable.events[0]).toMatchObject({
      catalog: 'available', preparable: 'conditional', pinnedSnapshot: 'unavailable',
    })
  })

  it('still records server understanding when capability metadata cannot be loaded', () => {
    const status = assessStrategyCapabilities(draft, undefined, 'live')
    expect(status.stages[0]).toMatchObject({
      key: 'understand', state: 'available', detail: '服务端已生成最终策略规则',
    })
    expect(status.canRun).toBe(false)
  })

  it('uses per-event capabilities instead of a frontend annual-report allowlist', () => {
    const quarterlyDraft: StrategyDraft = {
      ...draft,
      entry: {
        operator: 'all',
        conditions: [{
          id: 'entry-quarterly', kind: 'event',
          eventCode: 'event.financial_results.quarterly_report',
          label: '季度报告发布', trigger: '按首次可获得时间确认', attributes: {},
        }],
      },
      strategySpec: {
        ...draft.strategySpec,
        entry: {
          type: 'event_condition',
          event_code: 'event.financial_results.quarterly_report',
          definition_version: '1.0.0', trigger: 'published', attributes: {},
        },
      },
    }
    const quarterlyCapabilities: CapabilitiesResponse = {
      markets: ['CN_A'], input_modes: ['natural_language_zh'],
      strategy_scopes: ['single_instrument', 'long_only'],
      indicators: [{
        indicator_id: 'technical.macd', definition_version: '1.0.0', status: 'stable',
        display_name: 'MACD', description: 'MACD 指标', warmup_bars: 35,
        timeframes: ['1d'], evaluation_modes: ['bar_close_confirmed'],
        triggers: ['death_cross'], parameters: [], trigger_definitions: [],
      }],
      events: [{
        event_code: 'event.financial_results.quarterly_report', definition_version: '1.0.0',
        catalog_status: 'stable', status: 'available', backtest_available: true,
        preparation_available: false, availability_scope: 'pinned_snapshot',
        unavailable_reason: null, triggers: ['published'],
      }],
      execution_policies: ['next_tradable_session_open'], event_catalog_status: 'published',
      event_backtest_available: true, event_preparation_available: false,
      event_availability_scope: 'pinned_snapshot', backtest_execution_available: true,
      limits: { max_body_bytes: 16_384, max_utterance_characters: 2000,
        max_instrument_context_characters: 32 },
    }

    const status = assessStrategyCapabilities(quarterlyDraft, quarterlyCapabilities, 'live')
    expect(status.canRun).toBe(true)
    expect(status.reason).toBeNull()
    expect(status.events).toEqual([expect.objectContaining({
      eventCode: 'event.financial_results.quarterly_report',
      label: '季度报告',
      catalog: 'available',
      preparable: 'available',
      pinnedSnapshot: 'available',
      detail: '当前固定快照已覆盖。',
    })])
  })

  it('keeps the report term threshold and fill-anchored session exit visible on the strategy card', () => {
    const summary = toStrategySummary({
      ...draft,
      entry: {
        operator: 'all',
        conditions: [{
          id: 'entry-event-text',
          kind: 'event',
          eventCode: 'event.financial_results.annual_report',
          label: '年度报告正文中“AI”完整词出现 > 5 次',
          trigger: '按首次可获得时间确认',
          attributes: {},
        }],
      },
      exit: {
        operator: 'first_of',
        conditions: [{
          id: 'exit-hold-3',
          kind: 'holding_period',
          label: '实际买入成交后第 3 个交易日卖出',
          trigger: '从首次买入成交后的下一交易日起计',
          sessions: 3,
          anchor: 'first_entry_fill',
          countMode: 'subsequent_trading_sessions',
          execution: 'target_session_open_proxy',
        }],
      },
    })
    expect(summary.rows).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: '买入', value: '年度报告正文中“AI”完整词出现 > 5 次' }),
      expect.objectContaining({ label: '卖出', value: '实际买入成交后第 3 个交易日卖出' }),
    ]))
  })

  it('renders position return and trailing drawdown exits without treating them as condition groups', () => {
    const positionExitDraft: StrategyDraft = {
      ...draft,
      exit: {
        operator: 'first_of',
        conditions: [
          {
            id: 'exit-profit', kind: 'position_return', exitTrigger: 'take_profit',
            label: '持仓收益达到 33% 止盈',
            trigger: '以后复权日线收盘确认', thresholdPct: 33,
            anchor: 'first_entry_fill', observation: 'back_adjusted_daily_close',
            evaluationMode: 'bar_close_confirmed', execution: 'next_tradable_session_open',
            editable: false,
          },
          {
            id: 'exit-trailing', kind: 'trailing_drawdown',
            label: '持仓后收盘高点回撤 3% 卖出',
            trigger: '以后复权日线收盘确认', thresholdPct: 3,
            anchor: 'first_entry_fill', peakBasis: 'back_adjusted_daily_close',
            evaluationMode: 'bar_close_confirmed', execution: 'next_tradable_session_open',
            editable: false,
          },
        ],
      },
      strategySpec: {
        ...draft.strategySpec,
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
      },
    }

    const rules = strategyRuleTrees(positionExitDraft)
    expect(rules.exit).toMatchObject({
      kind: 'group', operator: 'first_of', children: [
        { kind: 'leaf', label: '持仓收益达到 33% 止盈' },
        { kind: 'leaf', label: '持仓后收盘高点回撤 3% 卖出' },
      ],
    })
    expect(summarizeRule(rules.exit)).toBe(
      '任一先发生（持仓收益达到 33% 止盈 / 持仓后收盘高点回撤 3% 卖出）',
    )
  })

  it('keeps the API drawdown path instead of deriving it from presentation returns', () => {
    const series = toSeries([
      { date: '2024-03-14', equity: 100, benchmark: 100, drawdown: 0 },
      { date: '2024-03-15', equity: 92, benchmark: 95, drawdown: -0.08 },
    ])
    expect(series[1]).toMatchObject({ equity: 92, drawdown: -8 })
    expect(series[1]?.strategy).toBeCloseTo(-8)
    expect(series[1]?.benchmark).toBeCloseTo(-5)
  })

  it('projects point details from the related signal, order and fill without inventing effects', () => {
    const series = toSeries([
      { date: '2024-03-14', equity: 100, benchmark: 100, drawdown: 0 },
      { date: '2024-03-15', equity: 101, benchmark: 100.5, drawdown: 0 },
    ])
    const [mark] = toChartMarks(series, activities)
    expect(mark).toMatchObject({
      kind: 'buy', signalAt: '2024-03-14T20:57:33+08:00',
      orderAt: '2024-03-15T09:15:00+08:00', fillAt: '2024-03-15T09:30:00+08:00',
      capacityImpact: '上一交易日已完成成交量 × 参与率',
      timeQuality: 'daily_bar_open_proxy',
      timeSemantics: '日线 OHLCV 的开盘价代理记录边界，不代表真实逐笔成交',
    })
    expect(mark?.evidence[0]?.provider).toBe('eastmoney')
    expect(mark?.priceLimitImpact).toBe('活动记录未标记涨跌停影响')
    expect(mark?.tPlusOneImpact).toBe('活动记录未标记次日可卖限制')
  })

  it('locates minute fills on their own timestamp instead of the first point', () => {
    const series = toSeries(['09:30', '09:31', '09:32'].map(time => ({
      date: `2026-09-11T${time}:00+08:00`, equity: 100, benchmark: 100, drawdown: 0,
    })))
    const marks = toChartMarks(series, [{ ...activities[2]!, kind: 'fill',
      occurredAt: '2026-09-11T09:32:00+08:00' }])
    expect(marks[0]?.index).toBe(2)
  })

  it('translates execution reason codes into readable price-limit impact', () => {
    const series = toSeries([
      { date: '2024-03-14', equity: 100, benchmark: 100, drawdown: 0 },
      { date: '2024-03-15', equity: 100, benchmark: 100.5, drawdown: 0 },
    ])
    const marks = toChartMarks(series, [
      activities[0]!,
      activities[1]!,
      {
        id: 'unfilled:order-1', kind: 'unfilled', occurredAt: '2024-03-15T09:30:00+08:00',
        side: 'buy', title: '买入未成交', status: 'expired', reason: 'one_price_limit_up',
        chainId: 'decision-1', decisionId: 'decision-1', orderId: 'order-1',
        parentId: 'order:order-1', quantity: 3_800,
      },
    ])

    expect(marks[0]?.kind).toBe('no_fill')
    expect(marks[0]?.priceLimitImpact).toBe('受涨停限制；日线数据无法证明排队位置')
  })

  it('builds utterance to condition, signal, decision, order, fill and account nodes', () => {
    const series = toSeries([
      { date: '2024-03-14', equity: 100, benchmark: 100, drawdown: 0 },
      { date: '2024-03-15', equity: 101, benchmark: 100.5, drawdown: 0 },
    ])
    const selected = toTradeRows(activities).at(-1)
    if (!selected) throw new Error('expected a selected activity')
    const chain = buildChain(draft, activities, selected, { metrics, series, evidence: runEvidence })

    expect(chain.map((node) => node.kind)).toEqual([
      'utterance', 'normalized', 'signal', 'decision', 'order', 'fill', 'cash',
    ])
    expect(chain.find((node) => node.kind === 'signal')?.facts).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: '事件来源', value: '东方财富公告' }),
      expect.objectContaining({ label: '时间质量', value: '供应商秒级首次可得' }),
      expect.objectContaining({ label: '校验状态', value: '已通过来源校验' }),
      expect.objectContaining({ label: '信号语义', value: '事件信号' }),
      expect.objectContaining({ label: '信号有效期', value: '1 个交易日委托机会' }),
    ]))
    expect(chain.find((node) => node.kind === 'signal')?.technicalFacts)
      .toEqual(expect.arrayContaining([
        { label: '数据来源代码', value: 'eastmoney' },
        { label: '原始响应校验', value: evidence.rawResponseSha256 },
      ]))
    expect(selected).toMatchObject({
      timeQuality: 'daily_bar_open_proxy',
      timeSemantics: '日线 OHLCV 的开盘价代理记录边界，不代表真实逐笔成交',
    })
    expect(chain.find((node) => node.kind === 'fill')?.facts).toEqual(expect.arrayContaining([
      { label: '成交时间质量', value: '日线开盘价代理（时间非精确）' },
      { label: '成交时间说明', value: '日线 OHLCV 的开盘价代理记录边界，不代表真实逐笔成交' },
      { label: '委托尝试', value: '第 1 次' },
      { label: '本次结果', value: '本次委托按日线开盘价代理成交' },
    ]))
    expect(chain.find((node) => node.kind === 'fill')?.technicalFacts)
      .toEqual(expect.arrayContaining([
        { label: '源信号编号', value: 'signal:decision-1' },
        { label: '本次结果代码', value: 'matched_at_open' },
      ]))
    expect(chain.at(-1)?.facts).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: '该交易日净值', value: '101.00（起点 100）' }),
      expect.objectContaining({ label: '回测期末资产', value: '¥975,000.00' }),
      expect.objectContaining({ label: '数据快照', value: 'composite:v2' }),
    ]))
  })

  it('separates trusted date fallback from estimated or unverified event time', () => {
    const timingActivities: BacktestActivity[] = activities.map((activity) => activity.kind === 'signal'
      ? {
          ...activity,
          evidence: [
            {
              ...evidence,
              timeQuality: 'date_only_conservative',
              validationStatus: 'validated',
            },
            {
              ...evidence,
              id: 'AN-unverified',
              sourceEventId: 'AN-unverified',
              timeQuality: 'estimated_research_only',
              validationStatus: 'unverified',
            },
          ],
        }
      : activity)
    const selected = toTradeRows(timingActivities).at(-1)
    if (!selected) throw new Error('expected a selected activity')

    const facts = buildChain(draft, timingActivities, selected)
      .find((node) => node.kind === 'signal')?.facts

    expect(facts).toEqual(expect.arrayContaining([
      expect.objectContaining({
        label: '时间质量 1',
        value: '可信日期；按当日收盘后可得，下一交易日委托',
      }),
      expect.objectContaining({
        label: '时间质量 2',
        value: '估算时间（仅研究，不可交易）',
      }),
      expect.objectContaining({ label: '校验状态 1', value: '已通过来源校验' }),
      expect.objectContaining({ label: '校验状态 2', value: '未验证，不可交易' }),
    ]))
  })

  it('shows the original signal and prior no-fill reason for a retry attempt', () => {
    const retryActivities: BacktestActivity[] = activities.map((activity) => ({
      ...activity,
      signalSemantics: 'edge',
      signalValiditySessions: 3,
      attemptNo: activity.kind === 'signal' ? null : 2,
      retryReason: activity.kind === 'signal' ? null : 'one_price_limit_up',
    }))
    const selected = toTradeRows(retryActivities).at(-1)
    if (!selected) throw new Error('expected a selected retry activity')

    const chain = buildChain(draft, retryActivities, selected)
    const orderFacts = chain.find((node) => node.kind === 'order')?.facts

    expect(orderFacts).toEqual(expect.arrayContaining([
      { label: '信号语义', value: '边沿信号（只在条件由假变真时触发）' },
      { label: '信号有效期', value: '3 个交易日委托机会' },
      { label: '委托尝试', value: '第 2 次' },
      { label: '重试原因', value: '前次委托受一字涨停限制，本次继续尝试' },
    ]))
    expect(chain.find((node) => node.kind === 'order')?.technicalFacts)
      .toEqual(expect.arrayContaining([
        { label: '源信号编号', value: 'signal:decision-1' },
        { label: '重试原因代码', value: 'one_price_limit_up' },
      ]))
  })
})
