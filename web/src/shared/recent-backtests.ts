import type { BacktestActivity, BacktestReviewResponse, StrategyDraft } from './api/types'
import { pricePlanTitle } from './price-plan'
import type {
  BacktestMetrics, ChartMark, Instrument, RunEvidence, SeriesPoint, StrategySummary, TradeRow,
} from '../types'

/** Completed report data only. Conversation turns and request cursors stay in memory. */
export type CompletedReportSnapshot = {
  id: string
  draft: StrategyDraft
  instrument: Instrument
  strategy: StrategySummary
  metrics: BacktestMetrics
  series: SeriesPoint[]
  marks: ChartMark[]
  trades: TradeRow[]
  evidence: RunEvidence
  activities: BacktestActivity[]
  review?: BacktestReviewResponse
}

export type RecentBacktests = { reports: CompletedReportSnapshot[]; notice?: string }
type Mode = 'mock' | 'live'
export const MAX_RECENT_BACKTESTS = 10
export const MAX_RECENT_BACKTEST_BYTES = 2 * 1024 * 1024
export const recentBacktestsKey = (mode: Mode) => `ashare:recent-backtests:v1:${mode}`
const unavailable = '本机存储暂不可用，新记录仅在本次打开期间保留。'

// Detect damaged payloads, including edits deep inside a chart or a trade.
const checksum = (text: string): string => {
  let hash = 2166136261
  for (let index = 0; index < text.length; index += 1) {
    hash = Math.imul(hash ^ text.charCodeAt(index), 16777619)
  }
  return (hash >>> 0).toString(16)
}

const reportOnly = (source: CompletedReportSnapshot): CompletedReportSnapshot => {
  const draft = source.draft
  const plan = draft.strategySpec?.trading_plan
  const title = plan?.kind === 'grid' ? pricePlanTitle(plan) : source.strategy.title
  return JSON.parse(JSON.stringify({
    id: source.id,
    draft: {
      id: draft.id, revision: draft.revision, strategyHash: draft.strategyHash,
      sourceText: '', title: plan?.kind === 'grid' ? title : draft.title, instrument: draft.instrument,
      confidence: draft.confidence, entry: draft.entry, exit: draft.exit,
      execution: draft.execution, backtest: draft.backtest, assumptions: draft.assumptions,
      warnings: draft.warnings, strategySpec: draft.strategySpec,
    },
    instrument: source.instrument, strategy: { ...source.strategy, title }, metrics: source.metrics,
    series: source.series, marks: source.marks, trades: source.trades,
    evidence: source.evidence, activities: source.activities, review: source.review,
  })) as CompletedReportSnapshot
}

const encode = (reports: CompletedReportSnapshot[]): string => JSON.stringify({
  version: 1,
  records: reports.map(report => {
    const payload = JSON.stringify(report)
    return { payload, checksum: checksum(payload) }
  }),
})

const object = (value: unknown): value is Record<string, unknown> =>
  Boolean(value && typeof value === 'object' && !Array.isArray(value))

export function readRecentBacktests(mode: Mode): RecentBacktests {
  try {
    const text = localStorage.getItem(recentBacktestsKey(mode))
    if (!text) return { reports: [] }
    if (text.length * 2 > MAX_RECENT_BACKTEST_BYTES) throw new Error('Oversized history')
    const envelope: unknown = JSON.parse(text)
    if (!object(envelope) || envelope.version !== 1 || !Array.isArray(envelope.records)) {
      throw new Error('Invalid history')
    }
    let damaged = false
    const reports: CompletedReportSnapshot[] = []
    for (const record of envelope.records.slice(-MAX_RECENT_BACKTESTS)) {
      try {
        if (!object(record) || typeof record.payload !== 'string'
          || record.checksum !== checksum(record.payload)) throw new Error('Damaged record')
        const value: unknown = JSON.parse(record.payload)
        if (!object(value) || typeof value.id !== 'string'
          || !['draft', 'instrument', 'strategy', 'metrics', 'evidence'].every(key => object(value[key]))
          || !['series', 'marks', 'trades', 'activities'].every(key => Array.isArray(value[key]))) {
          throw new Error('Invalid report')
        }
        reports.push(reportOnly(value as CompletedReportSnapshot))
      } catch { damaged = true }
    }
    return { reports, ...(damaged ? { notice: '部分本机记录已损坏，已跳过；当前策略不受影响。' } : {}) }
  } catch (error) {
    return { reports: [], notice: error instanceof SyntaxError || error instanceof Error
      && ['Oversized history', 'Invalid history'].includes(error.message)
      ? '本机历史记录无法读取，已开启空白对话。' : unavailable }
  }
}

export function saveRecentBacktests(
  mode: Mode, previous: CompletedReportSnapshot[], incoming: CompletedReportSnapshot[],
): RecentBacktests {
  const reports = [...previous]
  try {
    let oversized = false
    for (const source of incoming) {
      const snapshot = reportOnly(source)
      if (encode([snapshot]).length * 2 > MAX_RECENT_BACKTEST_BYTES) {
        oversized = true
        continue
      }
      const index = reports.findIndex(item => item.id === source.id)
      if (index < 0) reports.push(snapshot)
      else reports[index] = snapshot
    }
    let bounded = reports.slice(-MAX_RECENT_BACKTESTS)
    while (bounded.length && encode(bounded).length * 2 > MAX_RECENT_BACKTEST_BYTES) {
      bounded = bounded.slice(1)
    }
    const notice = oversized
      ? '这份报告超出本机保存大小，刷新或新建策略后将无法再查看。' : undefined
    try {
      localStorage.setItem(recentBacktestsKey(mode), encode(bounded))
      return { reports: bounded, notice }
    } catch { return { reports: bounded, notice: unavailable } }
  } catch { return { reports: previous, notice: unavailable } }
}

export function clearRecentBacktests(mode: Mode): RecentBacktests {
  try {
    localStorage.removeItem(recentBacktestsKey(mode))
    return { reports: [] }
  } catch { return { reports: [], notice: '本页历史已清除；浏览器暂不允许清除本机存储。' } }
}

// Large completed curves belong in the browser database, not synchronous
// localStorage. Keep the old reader as migration/fallback, never truncate bars.
const openReportDatabase = (): Promise<IDBDatabase> => new Promise((resolve, reject) => {
  const request = indexedDB.open('ashare-completed-reports', 1)
  request.onupgradeneeded = () => request.result.createObjectStore('reports')
  request.onsuccess = () => resolve(request.result)
  request.onerror = () => reject(request.error)
  request.onblocked = () => reject(new Error('Report database blocked'))
})

async function reportDatabase(mode: Mode, reports?: CompletedReportSnapshot[]): Promise<CompletedReportSnapshot[]> {
  const database = await openReportDatabase()
  try {
    return await new Promise((resolve, reject) => {
      const transaction = database.transaction('reports', reports ? 'readwrite' : 'readonly')
      const store = transaction.objectStore('reports')
      const request = reports ? store.put(encode(reports), mode) : store.get(mode)
      let loaded: CompletedReportSnapshot[] = reports ?? []
      request.onsuccess = () => {
        if (reports || request.result == null) return
        try {
          const envelope = JSON.parse(String(request.result))
          if (envelope.version !== 1 || !Array.isArray(envelope.records)) throw new Error('Invalid history')
          loaded = envelope.records.slice(-MAX_RECENT_BACKTESTS).map((record: { payload: string; checksum: string }) => {
            if (typeof record.payload !== 'string' || record.checksum !== checksum(record.payload)) {
              throw new Error('Damaged record')
            }
            return reportOnly(JSON.parse(record.payload) as CompletedReportSnapshot)
          })
        } catch (error) { reject(error); transaction.abort() }
      }
      transaction.oncomplete = () => resolve(loaded)
      transaction.onerror = () => reject(transaction.error)
      transaction.onabort = () => reject(transaction.error ?? new Error('Report storage aborted'))
    })
  } finally { database.close() }
}

export async function loadCompletedReports(mode: Mode): Promise<RecentBacktests> {
  const legacy = readRecentBacktests(mode)
  try {
    const stored = await reportDatabase(mode)
    const merged = new Map(legacy.reports.map(report => [report.id, report]))
    stored.forEach(report => merged.set(report.id, report))
    return { reports: [...merged.values()].slice(-MAX_RECENT_BACKTESTS) }
  } catch {
    return { reports: legacy.reports, notice: unavailable }
  }
}

// Serialize writes so a slower earlier model-review update cannot overwrite
// a newer report. Each transaction merges against the latest persisted state.
let reportWrites: Promise<unknown> = Promise.resolve()
export function persistCompletedReports(
  mode: Mode, previous: CompletedReportSnapshot[], incoming: CompletedReportSnapshot[],
): Promise<RecentBacktests> {
  const operation = reportWrites.then(async (): Promise<RecentBacktests> => {
    const merged = new Map<string, CompletedReportSnapshot>()
    let storageFailed = false
    try { (await reportDatabase(mode)).forEach(report => merged.set(report.id, report)) }
    catch { storageFailed = true }
    for (const report of [...previous, ...incoming]) merged.set(report.id, reportOnly(report))
    const reports = [...merged.values()].slice(-MAX_RECENT_BACKTESTS)
    try {
      if (storageFailed) throw new Error('Report database unavailable')
      await reportDatabase(mode, reports)
      return { reports }
    } catch {
      // Preserve full reports in memory even if quota/private mode prevents saving.
      saveRecentBacktests(mode, previous, incoming)
      return { reports, notice: unavailable }
    }
  })
  reportWrites = operation.catch(() => undefined)
  return operation
}
