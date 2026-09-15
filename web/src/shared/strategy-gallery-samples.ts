import { fromLiveDraftResponse, type LiveDraftResponse } from './api/contract'
import type { BacktestActivity, BacktestRun, BacktestSummary, EquityPoint, Instrument } from './api/types'
import { CURATED_STRATEGIES } from './curated-strategies'
import type { CompletedReportSnapshot } from './recent-backtests'
import { toBacktestMetrics, toChartMarks, toRunEvidence, toSeries, toStrategySummary, toTradeRows, toUiInstrument } from '../view-model'

export type GallerySample = {
  strategyId: number
  status: 'ready' | 'unavailable'
  message?: string
  generatedAt?: string
  requestedRange?: { start: string; end: string }
  instrument?: Instrument
  snapshot?: CompletedReportSnapshot
  assumptions: string[]
  executionAvailable?: boolean
}

/** Public, precomputed samples; opening the library must never submit backtests. */
export type GallerySampleEntry = {
  strategyId: number
  status: 'ready' | 'unavailable'
  template: { name: string; buy: string; sell: string }
  generatedAt: string
  requestedRange: { start: string; end: string }
  instrument: Instrument
  sourceText: string
  message?: string
  assumptions?: string[]
  executionAvailable?: boolean
  unavailableReason?: string
  run?: BacktestRun
  draft?: LiveDraftResponse
  summary?: BacktestSummary
  series?: EquityPoint[]
  activities?: BacktestActivity[]
}

export function decodeGallerySamples(payload: unknown): GallerySample[] {
  if (!payload || typeof payload !== 'object' || !('schemaVersion' in payload)
    || payload.schemaVersion !== 'strategy-gallery-samples.v1'
    || !('entries' in payload) || !Array.isArray(payload.entries)) throw new Error('样例回测暂时无法读取，请重试。')
  const entries = payload.entries
  return CURATED_STRATEGIES.map(template => {
    const row = entries.find((entry: GallerySampleEntry) => entry?.strategyId === template.id) as GallerySampleEntry | undefined
    const base: GallerySample = { strategyId: template.id, status: 'unavailable',
      message: row?.message || '这条策略暂未生成可展示的样例回测。', assumptions: row?.assumptions ?? [],
      instrument: row?.instrument, generatedAt: row?.generatedAt, requestedRange: row?.requestedRange,
      executionAvailable: row?.executionAvailable }
    if (!row || row.status !== 'ready') return base
    try {
      const { run, draft, summary, series, activities } = row
      // Bind the result to the exact template revision, run and verified strategy.
      if (row.template?.name !== template.name || row.template.buy !== template.buy || row.template.sell !== template.sell)
        throw new Error('策略规则已更新，样例结果待重新生成。')
      const provenance = summary?.dataProvenance
      const evidenceMatches = summary?.runEvidence
        ? summary.runEvidence.strategyHash === draft?.strategy_hash
        : Boolean(run?.resultHash && provenance?.provider === 'eastmoney_mx_finance_data'
          && provenance.instrumentId === row.instrument.symbol && provenance.historyRows > 1
          && provenance.retrievedAt && provenance.historyStart <= row.requestedRange.start
          && provenance.historyEnd >= row.requestedRange.end)
      if (!run || run.state !== 'succeeded' || !run.resultAvailable || !summary || summary.runId !== run.id
        || !draft || draft.status !== 'ready' || !draft.strategy_hash
        || !evidenceMatches
        || !Array.isArray(series) || series.length < 2 || !Array.isArray(activities))
        throw new Error('样例结果尚未完整核验，暂不展示收益。')
      if ([summary.totalReturn, summary.winRate, summary.maxDrawdown, summary.annualizedReturn,
        summary.benchmarkReturn, summary.sharpeRatio].some(value => value !== null && !Number.isFinite(value))
        || series.some(point => !Number.isFinite(point.equity) || !Number.isFinite(point.drawdown)))
        throw new Error('样例结果包含缺失或异常数据，暂不展示收益。')
      const outcome = fromLiveDraftResponse(draft, { instrument: row.instrument, utterance: row.sourceText })
      if (outcome.status !== 'compiled' || outcome.draft.instrument.symbol !== row.instrument.symbol)
        throw new Error('样例标的与策略不一致，暂不展示收益。')
      const points = toSeries(series)
      const snapshot: CompletedReportSnapshot = {
        id: run.id, draft: outcome.draft, instrument: toUiInstrument(outcome.draft),
        strategy: toStrategySummary(outcome.draft), metrics: toBacktestMetrics(summary),
        series: points, activities, marks: toChartMarks(points, activities),
        trades: toTradeRows(activities), evidence: toRunEvidence(summary),
      }
      return { ...base, status: 'ready', message: undefined, snapshot }
    } catch (error) {
      return { ...base, message: error instanceof Error ? error.message : '样例结果暂不可用。' }
    }
  })
}

export async function fetchGallerySamples(signal: AbortSignal): Promise<GallerySample[]> {
  const response = await fetch(`${import.meta.env.BASE_URL}strategy-gallery-samples.json`, { signal, cache: 'no-cache' })
  if (!response.ok) throw new Error('样例回测暂时无法读取，请重试。')
  return decodeGallerySamples(await response.json())
}

export const sampleFrequency = (sample: GallerySample): string => {
  const spec = sample.snapshot?.draft.strategySpec
  if (spec?.trading_plan && spec.trading_plan.kind !== 'scheduled'
    && spec.trading_plan.parameters.observation === 'minute_bar') return '分钟回测'
  const frequency = spec?.execution.evaluation_frequency
  if (frequency === 'daily_close_and_minute_bar') return '日线＋分钟'
  if (frequency === '1d_close') return '日线回测'
  if (frequency === '1m_bar') return '分钟回测'
  return '粒度见报告'
}
