import { describe, expect, it } from 'vitest'

import type {
  BacktestActivity,
  BacktestSignalEvidence,
  CapabilitiesResponse,
  StrategyDraft,
} from './shared/api/types'
import type { BacktestMetrics, RunEvidence } from './types'
import {
  assessStrategyCapabilities,
  buildChain,
  secondaryMetric,
  strategyRuleTrees,
  summarizeRule,
  toChartMarks,
  toOrderRows,
  toSeries,
  toStrategySummary,
  toTradeRows,
} from './view-model'

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
            draft.strategySpec.entry,
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
