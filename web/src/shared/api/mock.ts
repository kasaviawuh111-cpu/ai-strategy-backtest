import type {
  BacktestActivity,
  BacktestRun,
  BacktestSummary,
  Clarification,
  CompileRequest,
  CompileResponse,
  EquityPoint,
  StrategyDraft,
  StrategySpec,
} from './types'
import { ApiError } from './types'
import {
  MAXIMUM_INITIAL_CASH_CNY,
  MINIMUM_INITIAL_CASH_CNY,
  STANDARD_INITIAL_CASH_CNY,
} from '../config/backtest'
import { strategySpecFromDraft } from './contract'

type MockRunRecord = {
  run: BacktestRun
  pollCount: number
  signalLabel: string
  initialCashCny: number
  eventStrategy: boolean
  eventCode: string | null
  documentTermHold: boolean
}

const runs = new Map<string, MockRunRecord>()
const MOCK_REFERENCE_INITIAL_CASH_CNY = 100_000
const MOCK_TOTAL_RETURN = -0.0254315

type MockStrategyKind =
  | 'annual_report'
  | 'annual_report_term_hold'
  | 'earnings_forecast_macd'
  | 'trend_combo'
  | 'moving_average'
  | 'rsi'
  | 'macd'

const browserWait = (milliseconds: number) =>
  new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds))

let waitForMockPresentation = browserWait

const wait = (milliseconds = 700) => waitForMockPresentation(milliseconds)

/** Test-only timing seam. Production and browser Mock journeys retain their delays. */
export const enableImmediateMockWaitForTests = () => {
  if (import.meta.env.MODE !== 'test') throw new Error('Mock timing can only be changed in tests')
  waitForMockPresentation = async () => undefined
}

export const resetMockWaitForTests = () => {
  waitForMockPresentation = browserWait
}

const isEventRequest = (request: CompileRequest) =>
  !request.utterance.includes('半年报')
  && !request.utterance.includes('半年度报告')
  && (request.utterance.includes('年报') || request.utterance.includes('年度报告'))

const isMovingAverageRequest = (request: CompileRequest) =>
  /20\s*日(?:均)?线|MA\s*20|均线/.test(request.utterance.toUpperCase())

const isRsiRequest = (request: CompileRequest) => /RSI/.test(request.utterance.toUpperCase())

const isMacdRequest = (request: CompileRequest) => /MACD/.test(request.utterance.toUpperCase())

const isTrendComboRequest = (request: CompileRequest) =>
  isMacdRequest(request)
  && isMovingAverageRequest(request)
  && request.utterance.includes('金叉')
  && /(站上|突破|上穿)/.test(request.utterance)
  && request.utterance.includes('死叉')

const isEarningsForecastMacdRequest = (request: CompileRequest) =>
  request.utterance.includes('业绩预告')
  && isMacdRequest(request)
  && request.utterance.includes('金叉')
  && request.utterance.includes('死叉')

const isAnnualReportTermCountRequest = (request: CompileRequest) => {
  const normalized = request.utterance.normalize('NFKC')
  const hasAiTerm = /(?:提到|出现|包含|正文(?:中)?)[“”"']?\s*AI\s*[“”"']?/i.test(normalized)
  const hasCountThreshold = /(?:次数|出现|提及)?\s*(?:超过|大于|多于|>)\s*5\s*次?/i.test(normalized)
  return isEventRequest(request) && hasAiTerm && hasCountThreshold
}

const isThreeSessionExitRequest = (request: CompileRequest) =>
  /(?:买入|成交|建仓)?(?:后)?\s*(?:第)?\s*3\s*(?:个)?\s*(?:交易日|交易天|天)(?:后)?\s*(?:卖出|平仓)/.test(
    request.utterance.normalize('NFKC'),
  )

const looksLikeUnsupportedEventRequest = (request: CompileRequest) =>
  /(半年报|半年度报告|季报|季度报告|业绩预告|业绩快报|获批|许可|中标|合同|回购|分红|增持|减持|解禁|处罚|立案|诉讼|重组)/.test(request.utterance)

const isSupportedMovingAverageRule = (request: CompileRequest) =>
  isMovingAverageRequest(request)
  && /(突破|上穿)/.test(request.utterance)
  && /(跌破|下穿)/.test(request.utterance)

const isSupportedRsiRule = (request: CompileRequest) =>
  isRsiRequest(request)
  && /(低于|小于|超卖)/.test(request.utterance)
  && /(高于|大于|超买)/.test(request.utterance)

const isSupportedMacdRule = (request: CompileRequest) =>
  isMacdRequest(request) && request.utterance.includes('金叉') && request.utterance.includes('死叉')

const hasCompleteTradeRule = (request: CompileRequest) =>
  /(买入|建仓)/.test(request.utterance) && /(卖出|平仓)/.test(request.utterance)

const strategyKind = (request: CompileRequest): MockStrategyKind | null => {
  const clarifiedMacd = hasCompleteTradeRule(request)
    && request.clarification?.id === 'macd_confirmation_time'
    && ['close', 'intrabar'].includes(request.clarification.choiceId)
    && isMacdRequest(request)
  if (!hasCompleteTradeRule(request) && !clarifiedMacd) return null
  if (isEarningsForecastMacdRequest(request)) return 'earnings_forecast_macd'
  if (looksLikeUnsupportedEventRequest(request)) return null
  if (isEventRequest(request)) {
    if (isAnnualReportTermCountRequest(request) && isThreeSessionExitRequest(request)) {
      return 'annual_report_term_hold'
    }
    return isMacdRequest(request) && request.utterance.includes('死叉')
      ? 'annual_report'
      : null
  }
  if (isTrendComboRequest(request)) return 'trend_combo'
  if (isSupportedMovingAverageRule(request)) return 'moving_average'
  if (isSupportedRsiRule(request)) return 'rsi'
  if (isSupportedMacdRule(request) || clarifiedMacd) return 'macd'
  return null
}

const makeStrategySpec = (request: CompileRequest, kind: MockStrategyKind): StrategySpec => ({
  schema_version: 'strategy.v1',
  catalog: { catalog_id: 'cn_a.signals', release_version: '2026.08.29' },
  instrument: {
    market: 'CN_A',
    symbol: request.instrument.symbol,
    position_mode: 'long_only',
  },
  entry: kind === 'earnings_forecast_macd'
    ? {
        type: 'all',
        children: [{
          type: 'event_condition',
          event_code: 'event.financial_results.earnings_forecast_published',
          definition_version: '1.0.0',
          trigger: 'published',
          attributes: {},
        }, {
          type: 'indicator_condition',
          indicator_id: 'technical.macd',
          definition_version: '1.0.0',
          params: { fast: 12, slow: 26, signal: 9 },
          timeframe: '1d',
          evaluation_mode: 'bar_close_confirmed',
          trigger: 'golden_cross',
          value: null,
        }],
      }
    : kind === 'trend_combo'
      ? {
          type: 'all',
          children: [{
            type: 'indicator_condition',
            indicator_id: 'technical.macd',
            definition_version: '1.0.0',
            params: { fast: 12, slow: 26, signal: 9 },
            timeframe: '1d',
            evaluation_mode: 'bar_close_confirmed',
            trigger: 'golden_cross',
            value: null,
          }, {
            type: 'indicator_condition',
            indicator_id: 'technical.ma',
            definition_version: '1.0.0',
            params: { period: 20, price_field: 'close' },
            timeframe: '1d',
            evaluation_mode: 'bar_close_confirmed',
            trigger: 'price_crosses_above',
            value: null,
          }],
        }
      : kind === 'annual_report' || kind === 'annual_report_term_hold'
    ? {
        type: 'event_condition',
        event_code: 'event.financial_results.annual_report',
        definition_version: '1.0.0',
        trigger: 'published',
        attributes: {},
        ...(kind === 'annual_report_term_hold'
          ? {
              document_text: {
                metric_id: 'document.literal_mention_count' as const,
                metric_version: '1.0.0' as const,
                term: 'AI',
                normalization: 'nfkc' as const,
                match_mode: 'ascii_token' as const,
                case_sensitive: false,
                comparator: 'gt' as const,
                value: 5,
              },
            }
          : {}),
      }
    : kind === 'moving_average'
      ? {
          type: 'indicator_condition',
          indicator_id: 'technical.ma',
          definition_version: '1.0.0',
          params: { period: 20, price_field: 'close' },
          timeframe: '1d',
          evaluation_mode: 'bar_close_confirmed',
          trigger: 'price_crosses_above',
          value: null,
        }
      : kind === 'rsi'
        ? {
            type: 'indicator_condition',
            indicator_id: 'technical.rsi',
            definition_version: '1.0.0',
            params: { period: 14 },
            timeframe: '1d',
            evaluation_mode: 'bar_close_confirmed',
            trigger: 'below',
            value: 30,
          }
        : {
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
    children: [kind === 'annual_report_term_hold'
      ? {
          type: 'holding_period_exit',
          sessions: 3,
          anchor: 'first_entry_fill',
          count_mode: 'subsequent_trading_sessions',
          execution: 'target_session_open_proxy',
        }
      : kind === 'moving_average'
      ? {
          type: 'indicator_condition',
          indicator_id: 'technical.ma',
          definition_version: '1.0.0',
          params: { period: 20, price_field: 'close' },
          timeframe: '1d',
          evaluation_mode: 'bar_close_confirmed',
          trigger: 'price_crosses_below',
          value: null,
        }
      : kind === 'rsi'
        ? {
            type: 'indicator_condition',
            indicator_id: 'technical.rsi',
            definition_version: '1.0.0',
            params: { period: 14 },
            timeframe: '1d',
            evaluation_mode: 'bar_close_confirmed',
            trigger: 'above',
            value: 70,
          }
        : {
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
    data_capability: kind === 'annual_report' || kind === 'annual_report_term_hold'
      || kind === 'earnings_forecast_macd'
      ? 'daily_ohlcv_events'
      : 'daily_ohlcv',
    execution_resolution: '1d',
    evaluation_frequency: kind === 'annual_report' || kind === 'annual_report_term_hold'
      || kind === 'earnings_forecast_macd'
      ? 'event_available_plus_1d_close'
      : '1d_close',
    position_policy: 'single_position_no_pyramiding',
    t_plus_one: true,
  },
  backtest: {
    start: '2021-08-06',
    end: '2026-08-06',
    initial_cash_cny: STANDARD_INITIAL_CASH_CNY,
  },
})

const makeDraft = (request: CompileRequest, kind: MockStrategyKind): StrategyDraft => {
  const intrabar = request.clarification?.choiceId === 'intrabar'
  const earningsForecastStrategy = kind === 'earnings_forecast_macd'
  const trendComboStrategy = kind === 'trend_combo'
  const eventStrategy = kind === 'annual_report' || kind === 'annual_report_term_hold'
    || earningsForecastStrategy
  const documentTextStrategy = kind === 'annual_report_term_hold'
  const movingAverageStrategy = kind === 'moving_average'
  const rsiStrategy = kind === 'rsi'

  return {
    id: 'draft_mock_demo_001',
    revision: 1,
    strategyHash: 'sha256:mock-demo-strategy-not-live-evidence',
    sourceText: request.utterance,
    title: eventStrategy
      ? earningsForecastStrategy
        ? '业绩预告 + MACD 共振 · 日线'
        : documentTextStrategy
        ? '年度报告正文词频 + 持有期退出 · 日线'
        : '年度报告 + MACD 规则 · 日线'
      : trendComboStrategy
        ? 'MACD + MA20 趋势共振 · 日线'
        : movingAverageStrategy
        ? '20 日均线突破 · 日线'
        : rsiStrategy
          ? 'RSI 超卖反转 · 日线'
          : 'MACD 趋势跟随 · 日线',
    instrument: request.instrument,
    confidence: null,
    entry: {
      operator: 'all',
      conditions: earningsForecastStrategy
        ? [{
            id: 'entry_earnings_forecast',
            kind: 'event',
            eventCode: 'event.financial_results.earnings_forecast_published',
            label: '业绩预告发布',
            trigger: '按首次可获得时间确认',
            attributes: {},
          }, {
            id: 'entry_macd_cross',
            kind: 'indicator',
            indicatorId: 'technical.macd',
            label: 'MACD 金叉',
            trigger: 'DIF 由下向上穿过 DEA',
            timeframe: '1d',
            evaluationMode: 'bar_close_confirmed',
            parameters: [
              { key: 'fast', label: '快线', value: 12, min: 2, max: 60 },
              { key: 'slow', label: '慢线', value: 26, min: 3, max: 120 },
              { key: 'signal', label: '信号线', value: 9, min: 2, max: 60 },
            ],
          }]
        : trendComboStrategy
          ? [{
              id: 'entry_macd_cross',
              kind: 'indicator',
              indicatorId: 'technical.macd',
              label: 'MACD 金叉',
              trigger: 'DIF 由下向上穿过 DEA',
              timeframe: '1d',
              evaluationMode: 'bar_close_confirmed',
              parameters: [
                { key: 'fast', label: '快线', value: 12, min: 2, max: 60 },
                { key: 'slow', label: '慢线', value: 26, min: 3, max: 120 },
                { key: 'signal', label: '信号线', value: 9, min: 2, max: 60 },
              ],
            }, {
              id: 'entry_ma20_cross',
              kind: 'indicator',
              indicatorId: 'technical.ma',
              label: '收盘突破 20 日均线',
              trigger: '收盘价由下向上穿过 MA20',
              timeframe: '1d',
              evaluationMode: 'bar_close_confirmed',
              parameters: [
                { key: 'period', label: '均线周期', value: 20, min: 2, max: 250 },
              ],
            }]
          : eventStrategy
        ? [{
            id: 'entry_annual_report',
            kind: 'event',
            eventCode: 'event.financial_results.annual_report',
            label: documentTextStrategy
              ? '年度报告正文中“AI”完整词出现 > 5 次'
              : '年度报告发布',
            trigger: documentTextStrategy
              ? '按首次可获得时间确认；使用固定正文提取与计数规则'
              : '按首次可获得时间确认',
            attributes: {},
            ...(documentTextStrategy
              ? { documentText: {
                  metric_id: 'document.literal_mention_count',
                  metric_version: '1.0.0',
                  term: 'AI',
                  normalization: 'nfkc',
                  match_mode: 'ascii_token',
                  case_sensitive: false,
                  comparator: 'gt',
                  value: 5,
                } }
              : {}),
          }]
        : movingAverageStrategy
          ? [{
              id: 'entry_ma20_cross',
              kind: 'indicator',
              indicatorId: 'technical.ma',
              label: '收盘突破 20 日均线',
              trigger: '收盘价由下向上穿过 MA20',
              timeframe: '1d',
              evaluationMode: 'bar_close_confirmed',
              parameters: [
                { key: 'period', label: '均线周期', value: 20, min: 2, max: 250 },
              ],
            }]
          : rsiStrategy
            ? [{
                id: 'entry_rsi_below',
                kind: 'indicator',
                indicatorId: 'technical.rsi',
                label: 'RSI 低于 30',
                trigger: 'RSI(14) 低于 30',
                timeframe: '1d',
                evaluationMode: 'bar_close_confirmed',
                parameters: [
                  { key: 'period', label: '周期', value: 14, min: 2, max: 250 },
                  { key: '$value', label: '买入阈值', value: 30, min: 0, max: 100 },
                ],
              }]
            : [{
              id: 'entry_macd_cross',
              kind: 'indicator',
              indicatorId: 'technical.macd',
              label: 'MACD 金叉',
              trigger: 'DIF 由下向上穿过 DEA',
              timeframe: '1d',
              evaluationMode: intrabar ? 'intrabar_streaming' : 'bar_close_confirmed',
              parameters: [
                { key: 'fast', label: '快线', value: 12, min: 2, max: 60 },
                { key: 'slow', label: '慢线', value: 26, min: 3, max: 120 },
                { key: 'signal', label: '信号线', value: 9, min: 2, max: 60 },
              ],
              }],
    },
    exit: {
      operator: 'first_of',
      conditions: documentTextStrategy
        ? [{
            id: 'exit_holding_period_3',
            kind: 'holding_period',
            label: '实际买入成交后第 3 个交易日卖出',
            trigger: '从首次买入成交后的下一交易日起计，第 3 个 A 股交易日使用开盘价代理尝试卖出',
            sessions: 3,
            anchor: 'first_entry_fill',
            countMode: 'subsequent_trading_sessions',
            execution: 'target_session_open_proxy',
          }]
        : movingAverageStrategy
        ? [{
            id: 'exit_ma20_cross',
            kind: 'indicator',
            indicatorId: 'technical.ma',
            label: '收盘跌破 20 日均线',
            trigger: '收盘价由上向下穿过 MA20',
            timeframe: '1d',
            evaluationMode: 'bar_close_confirmed',
            parameters: [
              { key: 'period', label: '均线周期', value: 20, min: 2, max: 250 },
            ],
          }]
        : rsiStrategy
          ? [{
              id: 'exit_rsi_above',
              kind: 'indicator',
              indicatorId: 'technical.rsi',
              label: 'RSI 高于 70',
              trigger: 'RSI(14) 高于 70',
              timeframe: '1d',
              evaluationMode: 'bar_close_confirmed',
              parameters: [
                { key: 'period', label: '周期', value: 14, min: 2, max: 250 },
                { key: '$value', label: '卖出阈值', value: 70, min: 0, max: 100 },
              ],
            }]
          : [{
          id: 'exit_macd_cross',
          kind: 'indicator',
          indicatorId: 'technical.macd',
          label: 'MACD 死叉',
          trigger: 'DIF 由上向下穿过 DEA',
          timeframe: '1d',
          evaluationMode: intrabar ? 'intrabar_streaming' : 'bar_close_confirmed',
          parameters: [
            { key: 'fast', label: '快线', value: 12, min: 2, max: 60 },
            { key: 'slow', label: '慢线', value: 26, min: 3, max: 120 },
            { key: 'signal', label: '信号线', value: 9, min: 2, max: 60 },
          ],
            }],
    },
    execution: {
      entryPolicy: 'next_tradable_session_open',
      exitPolicy: 'next_tradable_session_open',
      priceLimitMode: 'wait_for_unlock',
      tPlusOne: true,
      dataCapability: eventStrategy ? 'daily_ohlcv_events' : 'daily_ohlcv',
      evaluationFrequency: eventStrategy ? 'event_available_plus_1d_close' : '1d_close',
      capacityMode: 'point_in_time_volume',
      participationRate: 0.05,
      allocationRatio: 1,
      commissionRate: 0.0003,
      minimumCommissionCny: 5,
      slippageBps: 5,
      retryUnfilledExits: true,
      maxExitAttempts: 20,
      warmupCalendarDays: 180,
      settlementExtensionDays: 14,
      runRobustness: true,
    },
    backtest: {
      start: '2021-08-06',
      end: '2026-08-06',
      initialCashCny: STANDARD_INITIAL_CASH_CNY,
    },
    assumptions: [
      eventStrategy
        ? '事件按首次可获得时间确认，不倒填公告日期；成交只使用日线开盘价代理，时间非精确'
        : intrabar
          ? '盘中触发；需要分钟级数据验证信号时点'
          : '日线收盘确认信号，下一交易日只使用开盘价代理，时间非精确',
      '遵守 A 股次日可卖规则；当天买入的股票下一交易日才可卖出',
      '买入默认使用 100% 可用资金，按 100 股整手向下取整；未投入现金继续保留',
      '涨停买入或跌停卖出时等待开板；仅日线数据无法证明开板则保守记为未成交',
    ],
    warnings: [
      ...(documentTextStrategy
        ? ['界面预览仅展示规则识别与卡片结构，没有读取年度报告正文，也不代表已计算词频或回测结果。']
        : []),
      ...(intrabar ? ['当前样例只有日线数据，盘中信号将在正式运行前被能力检查拒绝。'] : []),
    ],
    strategySpec: makeStrategySpec(request, kind),
  }
}

const clarification: Clarification = {
  id: 'macd_confirmation_time',
  question: 'MACD 金叉按哪个时点确认？',
  reason: '盘中可能短暂金叉后又消失，不同确认方式会改变信号和成交时间。',
  choices: [
    {
      id: 'close',
      label: '日线收盘确认',
      description: '信号稳定，下一交易日使用开盘价代理；成交时间不能精确到分秒。',
      recommended: true,
    },
    {
      id: 'intrabar',
      label: '盘中实时触发',
      description: '更早，但需要分钟数据，且可能出现盘中假金叉。',
    },
  ],
}

const incompleteRuleClarification: Clarification = {
  id: 'strategy_rule_incomplete',
  question: '请一次写清什么时候买入、什么时候卖出。',
  reason: '只有指标名称不能形成交易策略，系统不会替你补默认买卖规则。',
  choices: [{
    id: 'edit-utterance',
    label: '补充完整规则',
    description: '返回输入框，例如写成“MACD 金叉买入，死叉卖出”。',
    recommended: true,
    action: 'edit_utterance',
  }],
}

const makeSeries = (): EquityPoint[] => {
  const dates = [
    '2021-08', '2021-12', '2022-04', '2022-08', '2022-12', '2023-04',
    '2023-08', '2023-12', '2024-04', '2024-08', '2024-12', '2025-04',
    '2025-08', '2025-12', '2026-04', '2026-08',
  ]
  const equity = [100, 110, 112, 68, 64, 58, 56, 46.8, 52, 105, 102, 98, 101, 96, 94, 97.45685]
  const benchmark = [100, 105, 95, 75, 70, 62, 55, 48, 40, 80, 74, 72, 68, 65, 62, 60.77]
  let peak = equity[0] ?? 100

  return dates.map((date, index) => {
    const currentEquity = equity[index] ?? peak
    peak = Math.max(peak, currentEquity)
    return {
      date,
      equity: currentEquity,
      benchmark: benchmark[index] ?? 100,
      drawdown: currentEquity / peak - 1,
    }
  })
}

const baseActivities = (
  eventStrategy: boolean,
  documentTermHold: boolean,
  eventCode: string | null,
): BacktestActivity[] => {
  const earningsForecast = eventCode === 'event.financial_results.earnings_forecast_published'
  const activities: BacktestActivity[] = [
  {
    id: 'sig_01', chainId: 'decision_buy_01', decisionId: 'decision_buy_01', kind: 'signal',
    occurredAt: eventStrategy ? '2024-03-14T20:57:33+08:00' : '2022-05-13T15:00:00+08:00', side: 'buy',
    title: documentTermHold
      ? '年度报告正文词频条件确认'
      : earningsForecast
        ? '业绩预告发布且 MACD 金叉'
        : eventStrategy ? '年度报告首次可得' : 'MACD 金叉确认', status: 'confirmed',
    reason: documentTermHold
      ? '固定样例：先按事件首次可得时间确认，再按固定规则展示“AI”完整词出现 > 5 次。没有读取正文。'
      : eventStrategy
      ? '固定样例：按事件首次可得时间触发，不倒填公告日期。'
      : 'DIF 0.318 上穿 DEA 0.301，使用当日收盘数据。',
    evidence: eventStrategy ? [{
      type: 'event_observation',
      id: earningsForecast ? 'mock:earnings-forecast-example' : 'mock:annual-report-example',
      sourceEventId: earningsForecast
        ? 'mock:earnings-forecast-example'
        : 'mock:annual-report-example',
      provider: 'mock_sample',
      sourceUrl: null,
      availableAt: '2024-03-14T20:57:33+08:00',
      timeQuality: 'vendor_observed',
      timestampPrecision: 'second',
      validationStatus: 'demonstration_only',
      rawResponseSha256: null,
    }] : [{
      type: 'daily_bar_snapshot',
      id: 'mock:daily-bar-example',
      provider: 'mock_sample',
      availableAt: '2022-05-13T15:00:00+08:00',
      timeQuality: 'exact',
      validationStatus: 'demonstration_only',
      rawResponseSha256: null,
    }],
  },
  {
    id: 'ord_01', chainId: 'decision_buy_01', decisionId: 'decision_buy_01', orderId: 'order_buy_01',
    parentId: 'sig_01', kind: 'order', occurredAt: eventStrategy ? '2024-03-15T09:15:00+08:00' : '2022-05-16T09:15:00+08:00', side: 'buy',
    title: '提交买入委托', price: 22.84, quantity: 4300, status: 'submitted',
    reason: '模拟生成当日日单；成交只使用日线开盘价代理。',
  },
  {
    id: 'fill_01', chainId: 'decision_buy_01', decisionId: 'decision_buy_01', orderId: 'order_buy_01', fillId: 'fill_buy_01',
    parentId: 'ord_01', kind: 'fill', occurredAt: eventStrategy ? '2024-03-15T09:30:00+08:00' : '2022-05-16T09:30:00+08:00', side: 'buy',
    title: '买入成交', price: 22.86, quantity: 4300, status: 'filled',
    reason: '使用日线开盘价代理；成交价含 5 个基点滑点，费用另计。',
    capacityReasonCode: 'capacity_previous_session_volume_proxy',
    timeQuality: 'daily_bar_open_proxy',
    timeSemantics: '日线开盘、最高、最低、收盘与成交量数据中的开盘价代理记录边界，不代表已观测到该时刻的真实成交',
  },
  {
    id: 'sig_02', chainId: 'decision_sell_01', decisionId: 'decision_sell_01', kind: 'signal',
    occurredAt: documentTermHold
      ? '2024-03-20T09:15:00+08:00'
      : eventStrategy ? '2024-07-01T15:00:00+08:00' : '2023-02-02T15:00:00+08:00', side: 'sell',
    title: documentTermHold ? '持有 3 个交易日退出' : 'MACD 死叉确认', status: 'confirmed',
    reason: documentTermHold
      ? '固定样例：从首次实际买入成交后的下一交易日起计，到第 3 个 A 股交易日退出。'
      : 'DIF 0.412 下穿 DEA 0.428。',
  },
  {
    id: 'ord_02', chainId: 'decision_sell_01', decisionId: 'decision_sell_01', orderId: 'order_sell_01',
    parentId: 'sig_02', kind: 'order', occurredAt: documentTermHold
      ? '2024-03-20T09:15:00+08:00'
      : eventStrategy ? '2024-07-02T09:15:00+08:00' : '2023-02-03T09:15:00+08:00', side: 'sell',
    title: '提交卖出委托', price: 25.18, quantity: 4300, status: 'submitted',
    reason: documentTermHold
      ? '达到持有期退出日后提交卖出委托。'
      : 'MACD 死叉确认后的下一可交易日提交卖出委托。',
  },
  {
    id: 'fill_02', chainId: 'decision_sell_01', decisionId: 'decision_sell_01', orderId: 'order_sell_01', fillId: 'fill_sell_01',
    parentId: 'ord_02', kind: 'fill', occurredAt: documentTermHold
      ? '2024-03-20T09:30:00+08:00'
      : eventStrategy ? '2024-07-02T09:30:00+08:00' : '2023-02-03T09:30:00+08:00', side: 'sell',
    title: '卖出成交', price: 25.17, quantity: 4300, status: 'filled',
    reason: '持仓已满足 A 股次日可卖规则；成交价使用日线开盘价代理。',
    capacityReasonCode: 'capacity_previous_session_volume_proxy',
    timeQuality: 'daily_bar_open_proxy',
    timeSemantics: '日线开盘、最高、最低、收盘与成交量数据中的开盘价代理记录边界，不代表已观测到该时刻的真实成交',
  },
  {
    id: 'sig_03', chainId: 'decision_buy_blocked', decisionId: 'decision_buy_blocked', kind: 'signal',
    occurredAt: '2024-10-07T15:00:00+08:00', side: 'buy', title: 'MACD 金叉确认', status: 'confirmed',
    reason: 'DIF 由下向上穿过 DEA，买入信号成立。',
  },
  {
    id: 'ord_03', chainId: 'decision_buy_blocked', decisionId: 'decision_buy_blocked', orderId: 'order_buy_blocked',
    parentId: 'sig_03', kind: 'order', occurredAt: '2024-10-08T09:15:00+08:00', side: 'buy',
    title: '提交买入委托', price: 19.37, quantity: 5100, status: 'submitted',
    reason: '信号确认后的下一可交易日提交买入委托。',
  },
  {
    id: 'reject_01', chainId: 'decision_buy_blocked', decisionId: 'decision_buy_blocked', orderId: 'order_buy_blocked',
    parentId: 'ord_03', kind: 'unfilled', occurredAt: '2024-10-08T09:30:00+08:00', side: 'buy',
    title: '涨停未成交', price: 19.37, quantity: 5100, status: 'cancelled',
    reason: '开盘一字涨停，日线数据无法证明实际排队位置，因此按保守规则不成交。',
  },
  {
    id: 'sig_04', chainId: 'decision_buy_partial', decisionId: 'decision_buy_partial', kind: 'signal',
    occurredAt: '2025-01-16T15:00:00+08:00', side: 'buy', title: 'MACD 金叉确认', status: 'confirmed',
    reason: 'DIF 由下向上穿过 DEA，买入信号成立。',
  },
  {
    id: 'ord_04', chainId: 'decision_buy_partial', decisionId: 'decision_buy_partial', orderId: 'order_buy_partial',
    parentId: 'sig_04', kind: 'order', occurredAt: '2025-01-17T09:15:00+08:00', side: 'buy',
    title: '提交买入委托', price: 18.91, quantity: 5200, status: 'submitted',
    reason: '信号确认后的下一可交易日提交买入委托。',
  },
  {
    id: 'fill_03', chainId: 'decision_buy_partial', decisionId: 'decision_buy_partial', orderId: 'order_buy_partial', fillId: 'fill_buy_partial',
    parentId: 'ord_04', kind: 'partial_fill', occurredAt: '2025-01-17T09:30:00+08:00', side: 'buy',
    title: '买入部分成交', price: 18.92, quantity: 3000, status: 'partially_filled',
    reason: '按委托前已知容量，以日线开盘价代理完成部分成交。', capacityReasonCode: 'capacity_previous_session_volume_proxy',
    timeQuality: 'daily_bar_open_proxy',
    timeSemantics: '日线开盘、最高、最低、收盘与成交量数据中的开盘价代理记录边界，不代表已观测到该时刻的真实成交',
  },
  {
    id: 'expire_01', chainId: 'decision_buy_partial', decisionId: 'decision_buy_partial', orderId: 'order_buy_partial',
    parentId: 'ord_04', kind: 'expired', occurredAt: '2025-01-17T15:00:00+08:00', side: 'buy',
    title: '剩余委托过期', price: 18.91, quantity: 2200, status: 'expired',
    reason: '当日剩余委托未成交，收盘后过期。', capacityReasonCode: 'capacity_previous_session_volume_proxy',
  },
  {
    id: 'sig_05', chainId: 'decision_sell_02', decisionId: 'decision_sell_02', kind: 'signal',
    occurredAt: '2026-06-11T15:00:00+08:00', side: 'sell', title: 'MACD 死叉确认', status: 'confirmed',
    reason: 'DIF 由上向下穿过 DEA，卖出信号成立。',
  },
  {
    id: 'ord_05', chainId: 'decision_sell_02', decisionId: 'decision_sell_02', orderId: 'order_sell_02',
    parentId: 'sig_05', kind: 'order', occurredAt: '2026-06-12T09:15:00+08:00', side: 'sell',
    title: '提交卖出委托', price: 30.47, quantity: 3000, status: 'submitted', reason: '下一可交易日提交卖出委托。',
  },
  {
    id: 'fill_04', chainId: 'decision_sell_02', decisionId: 'decision_sell_02', orderId: 'order_sell_02', fillId: 'fill_sell_02',
    parentId: 'ord_05', kind: 'fill', occurredAt: '2026-06-12T09:30:00+08:00', side: 'sell',
    title: '卖出成交', price: 30.46, quantity: 3000, status: 'filled',
    reason: '持仓已满足 A 股次日可卖规则；成交价使用日线开盘价代理。', capacityReasonCode: 'capacity_previous_session_volume_proxy',
    timeQuality: 'daily_bar_open_proxy',
    timeSemantics: '日线开盘、最高、最低、收盘与成交量数据中的开盘价代理记录边界，不代表已观测到该时刻的真实成交',
  },
  ]
  return eventStrategy
    ? activities.filter((item) => ['decision_buy_01', 'decision_sell_01'].includes(item.chainId ?? ''))
    : activities
}

const activitiesForCapital = (
  initialCashCny: number,
  eventStrategy: boolean,
  documentTermHold: boolean,
  eventCode: string | null,
): BacktestActivity[] =>
  baseActivities(eventStrategy, documentTermHold, eventCode).map((activity) => {
    if (activity.quantity == null) return activity
    const scaled = Math.floor(
      activity.quantity * initialCashCny / MOCK_REFERENCE_INITIAL_CASH_CNY / 100,
    ) * 100
    return { ...activity, quantity: Math.max(100, scaled) }
  })

export const mockApi = {
  async compile(request: CompileRequest): Promise<CompileResponse> {
    await wait()
    if (
      request.clarification
      && (
        request.clarification.id !== 'macd_confirmation_time'
        || !['close', 'intrabar'].includes(request.clarification.choiceId)
      )
    ) {
      throw new ApiError({
        type: 'about:blank',
        title: '无法使用这个补充选项',
        status: 422,
        detail: '这个补充选项不在当前预览契约内，请返回重新识别。',
        code: 'clarification_choice_not_supported',
      })
    }
    if (!request.clarification && request.utterance.trim().toUpperCase() === 'MACD') {
      return {
        status: 'needs_clarification',
        draftId: 'draft_incomplete_001',
        clarification: incompleteRuleClarification,
      }
    }
    const needsClarification = !request.clarification &&
      isMacdRequest(request)
      && request.utterance.includes('盘中')

    if (needsClarification) {
      return { status: 'needs_clarification', draftId: 'draft_pending_001', clarification }
    }
    const kind = strategyKind(request)
    if (!kind) {
      throw new ApiError({
        type: 'about:blank',
        title: '暂时无法识别这条规则',
        status: 422,
        detail: '没有识别到当前可执行的技术指标或公告事件。请写清何时买入、何时卖出和回测区间。',
        code: 'no_supported_signal_recognized',
      })
    }
    return { status: 'compiled', draft: makeDraft(request, kind) }
  },

  async revise(draft: StrategyDraft): Promise<StrategyDraft> {
    await wait(60)
    return {
      ...draft,
      revision: draft.revision + 1,
      strategySpec: strategySpecFromDraft(draft),
    }
  },

  async createRun(draft: StrategyDraft): Promise<BacktestRun> {
    await wait()
    if (
      !Number.isInteger(draft.backtest.initialCashCny)
      || draft.backtest.initialCashCny < MINIMUM_INITIAL_CASH_CNY
      || draft.backtest.initialCashCny > MAXIMUM_INITIAL_CASH_CNY
    ) {
      throw new Error('初始资金需要是 1 万元至 10 亿元之间的整数。')
    }
    const now = new Date().toISOString()
    const id = `run_${String(runs.size + 1).padStart(4, '0')}`
    const run: BacktestRun = {
      id,
      state: 'queued',
      progress: 8,
      progressLabel: '预览：准备样例数据',
      createdAt: now,
      updatedAt: now,
      fingerprint: 'mock:demo-only:not-replayable',
      error: null,
      resultAvailable: false,
    }
    const entryIndicator = draft.entry.conditions.find((condition) => condition.kind === 'indicator')
    const entryEvent = draft.entry.conditions.find((condition) => condition.kind === 'event')
    const signalLabel = draft.entry.conditions.some((condition) => condition.kind === 'event')
      ? '生成事件与技术样例信号'
      : `生成${entryIndicator?.label ?? '技术指标'}样例信号`
    runs.set(id, {
      run,
      pollCount: 0,
      signalLabel,
      initialCashCny: draft.backtest.initialCashCny,
      eventStrategy: draft.entry.conditions.some((condition) => condition.kind === 'event'),
      eventCode: entryEvent?.eventCode ?? null,
      documentTermHold: draft.exit.conditions.some((condition) => condition.kind === 'holding_period'),
    })
    return run
  },

  async getRun(runId: string): Promise<BacktestRun> {
    await wait(45)
    const stored = runs.get(runId)
    if (!stored) throw new Error(`Unknown mock run: ${runId}`)
    if (stored.run.state === 'cancelled') return stored.run

    stored.pollCount += 1
    const stages: Array<Pick<BacktestRun, 'state' | 'progress' | 'progressLabel'>> = [
      { state: 'running:data', progress: 22, progressLabel: '预览：载入样例行情' },
      { state: 'running:signal', progress: 43, progressLabel: `预览：${stored.signalLabel}` },
      { state: 'running:execution', progress: 68, progressLabel: '预览：生成样例成交' },
      { state: 'running:report', progress: 88, progressLabel: '预览：整理样例报告' },
      { state: 'succeeded', progress: 100, progressLabel: '预览完成' },
    ]
    const stage = stages[Math.min(stored.pollCount - 1, stages.length - 1)]
    if (stage) stored.run = { ...stored.run, ...stage, updatedAt: new Date().toISOString() }
    if (stored.run.state === 'succeeded') stored.run = { ...stored.run, resultAvailable: true }
    return stored.run
  },

  async cancelRun(runId: string): Promise<BacktestRun> {
    await wait(40)
    const stored = runs.get(runId)
    if (!stored) throw new Error(`Unknown mock run: ${runId}`)
    stored.run = {
      ...stored.run,
      state: 'cancelled',
      progressLabel: '预览已取消',
      updatedAt: new Date().toISOString(),
    }
    return stored.run
  },

  async getSummary(runId: string): Promise<BacktestSummary> {
    await wait(45)
    const stored = runs.get(runId)
    if (!stored) throw new Error(`Unknown mock run: ${runId}`)
    return {
      runId,
      totalReturn: MOCK_TOTAL_RETURN,
      benchmarkReturn: -0.3923,
      annualizedReturn: -0.005,
      maxDrawdown: -0.5819,
      sharpeRatio: 0.16,
      winRate: 0.459,
      tradeCount: stored.eventStrategy ? 1 : 37,
      initialCashCny: stored.initialCashCny,
      finalEquityCny: Number(
        (stored.initialCashCny * (1 + MOCK_TOTAL_RETURN)).toFixed(2),
      ),
      interpretation: '固定样例中，策略期内小幅亏损，但跌幅小于样例买入持有；最大回撤约 58.2%。',
      dataRange: { start: '2021-08-06', end: '2026-08-06', sessions: 1211 },
      warnings: [
        '固定样例：收益、交易与事件用于界面预览，不来自回测服务。',
        '历史表现不代表未来收益。',
      ],
      runEvidence: null,
    }
  },

  async getSeries(_runId: string): Promise<EquityPoint[]> {
    await wait(45)
    void _runId
    return makeSeries()
  },

  async getActivities(runId: string): Promise<BacktestActivity[]> {
    await wait(45)
    const stored = runs.get(runId)
    if (!stored) throw new Error(`Unknown mock run: ${runId}`)
    return activitiesForCapital(
      stored.initialCashCny,
      stored.eventStrategy,
      stored.documentTermHold,
      stored.eventCode,
    )
  },
}
