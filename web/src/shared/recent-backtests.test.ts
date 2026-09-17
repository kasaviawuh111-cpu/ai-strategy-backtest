import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { enableImmediateMockWaitForTests, mockApi, resetMockWaitForTests } from './api/mock'
import {
  clearRecentBacktests, MAX_RECENT_BACKTEST_BYTES, readRecentBacktests,
  recentBacktestsKey, saveRecentBacktests, type CompletedReportSnapshot,
  loadCompletedReports, persistCompletedReports,
  hasLocalConversation, localConversationJourneys,
} from './recent-backtests'
import {
  toBacktestMetrics, toChartMarks, toRunEvidence, toSeries, toStrategySummary, toTradeRows, toUiInstrument,
} from '../view-model'

let fixture: CompletedReportSnapshot
beforeAll(async () => {
  enableImmediateMockWaitForTests()
  const outcome = await mockApi.compile({ utterance: '东方财富5日均线上穿20日均线买入，下穿卖出',
    instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
  if (outcome.status !== 'compiled') throw new Error('Expected complete fixture')
  const draft = outcome.draft
  const run = await mockApi.createRun(draft)
  const [summary, rawSeries, activities] = await Promise.all([
    mockApi.getSummary(run.id), mockApi.getSeries(run.id), mockApi.getActivities(run.id),
  ])
  fixture = { id: run.id, draft, instrument: toUiInstrument(draft), strategy: toStrategySummary(draft),
    metrics: toBacktestMetrics(summary), evidence: toRunEvidence(summary), series: toSeries(rawSeries),
    marks: toChartMarks(toSeries(rawSeries), activities), trades: toTradeRows(activities), activities }
  resetMockWaitForTests()
})
afterEach(() => vi.restoreAllMocks())

describe('bounded browser report history', () => {
  it('refreshes grid display names from retained parameters without changing results', () => {
    const old = structuredClone(fixture)
    old.strategy.title = '网格交易'
    old.draft.strategySpec.trading_plan = { kind: 'grid', parameters: {
      spacing_mode: 'cny', spacing: 1, order_shares: 200,
    } }
    const before = structuredClone(old)
    const restored = saveRecentBacktests('mock', [], [old]).reports[0]!
    expect(restored.strategy.title).toBe('网格·1元·200股/格')
    expect(restored.metrics).toEqual(before.metrics)
    expect(restored.draft.strategySpec).toEqual(before.draft.strategySpec)
    expect(old).toEqual(before)
  })
  it('retains the full large report in memory when IndexedDB is unavailable', async () => {
    const large = { ...fixture, id: 'large-database-fallback',
      series: Array.from({ length: 60_000 }, () => fixture.series[0]!) }
    const next = await persistCompletedReports('mock', [], [large])
    expect(next.reports[0]?.series).toHaveLength(60_000)
    expect(next.notice).toMatch(/仅在本次打开期间/)
  })

  it('reads legacy reports when the database cannot be opened', async () => {
    saveRecentBacktests('mock', [], [fixture])
    const restored = await loadCompletedReports('mock')
    expect(restored.reports[0]?.id).toBe(fixture.id)
    expect(restored.notice).toMatch(/暂不可用/)
  })
  it('keeps ten completed snapshots per mode, persists a local conversation bundle, and clears only its own key', () => {
    localStorage.setItem('unrelated-setting', 'preserve')
    const input = Array.from({ length: 12 }, (_, index) => ({ ...fixture, id: `run-${index}`,
      utterance: 'private conversation', clarificationMessages: [{ role: 'assistant' as const, text: 'model reasoning' }],
      conversationJourneyIds: [`run-${index}`, 'missing-sibling'] }))
    const saved = saveRecentBacktests('mock', [], input)
    expect(saved.reports).toHaveLength(10)
    expect(readRecentBacktests('mock').reports.map(item => item.id)).toEqual(input.slice(-10).map(item => item.id))
    expect(readRecentBacktests('live').reports).toEqual([])
    const serialized = localStorage.getItem(recentBacktestsKey('mock'))!
    expect(serialized).toContain('private conversation')
    expect(serialized).toContain('model reasoning')
    expect(readRecentBacktests('mock').reports[0]?.draft.sourceText).toBe('')
    expect(readRecentBacktests('mock').reports[0]).toMatchObject({
      utterance: 'private conversation',
      clarificationMessages: [{ role: 'assistant', text: 'model reasoning' }],
      conversationJourneyIds: ['run-2', 'missing-sibling'],
    })
    saveRecentBacktests('live', [], [fixture])
    clearRecentBacktests('mock')
    expect(localStorage.getItem('unrelated-setting')).toBe('preserve')
    expect(readRecentBacktests('live').reports).toHaveLength(1)
    expect(readRecentBacktests('live').reports[0]?.utterance).toBeUndefined()
    expect(hasLocalConversation(readRecentBacktests('live').reports[0]!)).toBe(false)
  })

  it('loads older report-only snapshots and skips malformed conversation fields', () => {
    const saved = saveRecentBacktests('mock', [], [fixture]).reports[0]!
    expect(saved.utterance).toBeUndefined()
    expect(saved.clarificationMessages).toBeUndefined()
    expect(hasLocalConversation(saved)).toBe(false)
    expect(localConversationJourneys(saved, [saved])).toEqual([])

    const dirty = saveRecentBacktests('mock', [], [{
      ...fixture,
      id: 'dirty-conversation',
      utterance: 12 as unknown as string,
      fromPanelEdit: 'yes' as unknown as boolean,
      clarificationMessages: [
        { role: 'system', text: 'drop me' } as never,
        { role: 'user', text: 'keep me', data: { extra: true } } as never,
      ],
      conversationJourneyIds: [1, 'ok', ''] as unknown as string[],
    }]).reports[0]!
    expect(dirty.utterance).toBeUndefined()
    expect(dirty.fromPanelEdit).toBeUndefined()
    expect(dirty.clarificationMessages).toEqual([{ role: 'user', text: 'keep me' }])
    expect(dirty.conversationJourneyIds).toEqual(['ok'])
    expect(JSON.stringify(dirty.clarificationMessages)).not.toContain('extra')
  })

  it('restores sibling journeys from stored conversation ids when they still exist', () => {
    const first = { ...fixture, id: 'run-a', utterance: 'first rule',
      conversationJourneyIds: ['run-a', 'run-b'] }
    const second = { ...fixture, id: 'run-b', utterance: 'second rule', fromPanelEdit: true,
      clarificationMessages: [{ role: 'user' as const, text: '改周期' }],
      conversationJourneyIds: ['run-a', 'run-b'] }
    const saved = saveRecentBacktests('mock', [], [first, second]).reports
    expect(localConversationJourneys(saved[1]!, saved).map(item => item.id)).toEqual(['run-a', 'run-b'])
    expect(localConversationJourneys(saved[1]!, [saved[1]!]).map(item => item.id)).toEqual(['run-b'])
    expect(localConversationJourneys(saved[1]!, [saved[1]!])[0]).toMatchObject({
      utterance: 'second rule', fromPanelEdit: true,
      clarificationMessages: [{ role: 'user', text: '改周期' }],
    })
  })

  it('bounds actual stored size and preserves older reports when one report is too large', () => {
    const saved = saveRecentBacktests('mock', [], [fixture])
    const oversized = { ...fixture, id: 'oversized', strategy: { ...fixture.strategy,
      title: '大'.repeat(MAX_RECENT_BACKTEST_BYTES) } }
    const next = saveRecentBacktests('mock', saved.reports, [oversized])
    expect(next.reports).toEqual(saved.reports)
    expect(next.notice).toMatch(/超出本机保存大小/)
    expect(localStorage.getItem(recentBacktestsKey('mock'))!.length * 2).toBeLessThanOrEqual(MAX_RECENT_BACKTEST_BYTES)
  })

  it('ignores damaged deep payloads and handles invalid JSON without crashing', () => {
    saveRecentBacktests('mock', [], [fixture])
    const key = recentBacktestsKey('mock')
    localStorage.setItem(key, localStorage.getItem(key)!.replace('东方财富', '损坏字段'))
    expect(readRecentBacktests('mock')).toMatchObject({ reports: [], notice: expect.stringContaining('损坏') })
    localStorage.setItem(key, '{')
    expect(readRecentBacktests('mock')).toMatchObject({ reports: [], notice: expect.stringContaining('无法读取') })
  })

  it('retains bounded in-memory reports when quota or browser policy rejects persistence', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('Full', 'QuotaExceededError') })
    const saved = saveRecentBacktests('mock', [], [fixture])
    expect(saved.reports).toHaveLength(1)
    expect(saved.notice).toMatch(/仅在本次打开期间/)
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new DOMException('Denied', 'SecurityError') })
    expect(readRecentBacktests('mock')).toMatchObject({ reports: [], notice: expect.stringContaining('暂不可用') })
  })
})
