import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, vi } from 'vitest'

import App from './App'
import { backtestApi, strategyApi } from './shared/api/client'
import { saveRecentBacktests, recentBacktestsKey } from './shared/recent-backtests'
import type { BacktestOptimizationCandidate, BacktestReviewResponse } from './shared/api/types'
import { enableImmediateMockWaitForTests, mockApi, resetMockWaitForTests } from './shared/api/mock'
import { settleMockRunOnFirstPoll } from './test/mock-run'
import {
  toBacktestMetrics, toChartMarks, toRunEvidence, toSeries, toStrategySummary, toTradeRows, toUiInstrument,
} from './view-model'

const renderApp = () => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>,
  )
}

const reviewControls = () => within(screen.getByRole('complementary', { name: '策略审阅' }))
const detailControls = () => within(screen.getByRole('region', { name: '策略详情' }))

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  resetMockWaitForTests()
})

describe('append-only strategy conversation', () => {
  it('keeps navigation empty until there is a real current conversation', async () => {
    vi.stubGlobal('matchMedia', vi.fn(() => ({ matches: true, addEventListener: vi.fn(), removeEventListener: vi.fn() })))
    vi.spyOn(HTMLElement.prototype, 'getClientRects').mockReturnValue([new DOMRect(0, 0, 44, 44)] as unknown as DOMRectList)
    const user = userEvent.setup()
    const { container } = renderApp()
    const rail = screen.getByRole('complementary', { name: '策略与历史' })
    expect(container.querySelector('.topbar')).not.toBeInTheDocument()
    expect(within(rail).getByRole('button', { name: '新建' })).toBeEnabled()
    expect(within(rail).getByRole('heading', { name: '最近' })).toBeInTheDocument()
    expect(rail.querySelectorAll('.rail-item')).toHaveLength(0)
    expect(rail).not.toHaveTextContent(/回测策略|当前策略|空白对话|还没有已完成|最多保存|清除本机历史/)

    // jsdom keeps this mobile-only control hidden; inspect its declared label.
    const toggle = rail.querySelector<HTMLButtonElement>('.rail-history-toggle')!
    expect(toggle).toHaveAttribute('aria-label', '打开导航菜单')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(rail).toHaveAttribute('data-history-open', 'true')
    expect(toggle).toHaveAttribute('aria-label', '收起导航菜单')
    expect(screen.getByRole('dialog', { name: '策略与历史' })).toHaveAttribute('aria-modal', 'true')
    expect((container.querySelector('.dock') as HTMLElement).inert).toBe(true)
    expect(toggle).toHaveFocus()
    await user.tab()
    expect(within(rail).getByRole('button', { name: '新建' })).toHaveFocus()
    await user.tab()
    expect(within(rail).getByRole('button', { name: '策略广场' })).toHaveFocus()
    expect((container.querySelector('.strategy-gallery') as HTMLElement).inert).toBe(true)
    await user.tab()
    expect(toggle).toHaveFocus()
    await user.tab({ shift: true })
    expect(within(rail).getByRole('button', { name: '策略广场' })).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(rail).toHaveAttribute('data-history-open', 'false')
    expect(toggle).toHaveFocus()
    expect((container.querySelector('.dock') as HTMLElement).inert).not.toBe(true)
    expect((container.querySelector('.strategy-gallery') as HTMLElement).inert).not.toBe(true)

    const text = '研究东方财富的长期交易规则，先比较几种可能的买入条件'
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: text } })
    const current = within(rail).getByRole('button', { name: `${text}，当前` })
    expect(current).toHaveAttribute('title', text)
    expect(current.querySelector('small')).toHaveTextContent('当前')
    await user.click(within(rail).getByRole('button', { name: '新建' }))
    expect(rail.querySelectorAll('.rail-item')).toHaveLength(0)
    expect(screen.getByLabelText('交易规则')).toHaveValue('')
  })

  it('ignores a completed analysis response arriving after an explicit new strategy', async () => {
    settleMockRunOnFirstPoll()
    let complete!: (review: BacktestReviewResponse) => void
    const review = vi.spyOn(backtestApi, 'review').mockImplementation(() => new Promise(resolve => { complete = resolve }))
    const create = vi.spyOn(backtestApi, 'create')
    const compile = vi.spyOn(strategyApi, 'compile')
    const user = userEvent.setup()
    renderApp()
    const text = '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出'
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: text } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await reviewControls().findByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    await user.click(screen.getByRole('button', { name: 'AI 分析与优化' }))
    await waitFor(() => expect(review).toHaveBeenCalledTimes(1))
    const draft = create.mock.calls[0]![0]
    const run = await create.mock.results[0]!.value
    const candidate = (id: 'model-opt-1' | 'model-opt-2'): BacktestOptimizationCandidate => ({
      id, title: id, diagnosis: '测试', changeDimension: 'confirmation', expectedEffect: '测试',
      tradeoff: '测试', suggestedUtterance: text, strategy: draft.strategySpec,
      strategyHash: `sha256:${id}`, modelSuggested: true,
    })
    await user.click(screen.getByRole('button', { name: '新建', hidden: true }))
    expect(review.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
    await act(async () => complete({ runId: run.id, sourceResultHash: 'sha256:test',
      generatedAt: '2026-09-08T12:00:00Z', evidenceGrade: 'limited', evidenceReasons: ['组件测试'],
      analysis: '旧会话迟到的分析', conclusion: '旧会话结论',
      optimizationCandidates: [candidate('model-opt-1'), candidate('model-opt-2')],
      modelProvenance: { provider: 'test', model: 'test', promptVersion: 'test',
        schemaVersion: 'backtest-review.v1', responseHash: 'sha256:old-review' },
      disclaimer: '历史回测与模型建议仅用于研究，不构成投资建议或真实交易指令',
    }))
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: text } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(2))
    expect(compile.mock.calls[1]?.[1]).toBeUndefined()
    expect(compile.mock.calls[1]?.[0].relatedReviews).toEqual([])
    expect(screen.queryByText('旧会话迟到的分析')).not.toBeInTheDocument()
  }, 10_000)

  it('restores stored conversation turns from recent history after reload', async () => {
    settleMockRunOnFirstPoll()
    const user = userEvent.setup()
    const first = renderApp()
    const utterance = '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出'
    fireEvent.change(screen.getByLabelText('交易规则'), { target: { value: utterance } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await reviewControls().findByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    await waitFor(() => expect(localStorage.getItem(recentBacktestsKey('mock'))).toContain(utterance))
    expect(first.container.querySelectorAll('[data-current-conversation]')).toHaveLength(1)
    expect(first.container.querySelectorAll('[data-history-id]')).toHaveLength(0)
    first.unmount()
    const compile = vi.spyOn(strategyApi, 'compile')
    const review = vi.spyOn(backtestApi, 'review')
    const create = vi.spyOn(backtestApi, 'create')
    const restored = renderApp()
    expect(screen.getByLabelText('交易规则')).toHaveValue('')
    expect(screen.queryByText(/^查看(?:并修改|策略)$/)).not.toBeInTheDocument()
    expect(restored.container.querySelectorAll('[data-current-conversation]')).toHaveLength(0)
    const archived = restored.container.querySelector<HTMLButtonElement>('[data-history-id]')!
    const historyId = archived.getAttribute('data-history-id')
    expect(archived).toHaveAccessibleName(archived.title)
    expect(archived.title).toContain(' 至 ')
    expect(archived.querySelectorAll('small')).toHaveLength(0)
    const savedReports = localStorage.getItem(recentBacktestsKey('mock'))
    await user.click(screen.getByRole('button', { name: '策略广场' }))
    expect(screen.getByRole('heading', { name: '精选策略' })).toBeVisible()
    expect(archived).toBeVisible()
    expect(localStorage.getItem(recentBacktestsKey('mock'))).toBe(savedReports)
    await user.click(archived)
    expect(screen.getByText('只读历史')).toBeVisible()
    expect(detailControls().getByRole('heading', { name: '回测报告' })).toBeVisible()
    expect(detailControls().queryByRole('button', { name: '编辑策略' })).not.toBeInTheDocument()
    expect(detailControls().queryByRole('button', { name: 'AI 分析与优化' })).not.toBeInTheDocument()
    expect(review).not.toHaveBeenCalled()
    expect(create).not.toHaveBeenCalled()
    await user.click(detailControls().getByRole('button', { name: '回到对话' }))
    expect(restored.container.querySelector('#pg-chat')).toHaveAttribute('data-view', 'chat')
    expect(screen.getByText(utterance, { selector: '.stream p' })).toBeVisible()
    expect(restored.container.querySelector(`#journey-${historyId}`)).toBeTruthy()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: {
      value: '贵州茅台5日均线上穿20日均线买入，下穿卖出',
    } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(1))
    expect(compile.mock.calls[0]?.[1]).toBeUndefined()
    expect(compile.mock.calls[0]?.[0].relatedRunIds).toEqual([historyId])
    expect(screen.queryByRole('button', { name: '清空上下文' })).not.toBeInTheDocument()
  }, 10_000)

  it('opens older report-only snapshots without restoring chat turns', async () => {
    enableImmediateMockWaitForTests()
    const outcome = await mockApi.compile({
      utterance: '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出',
      instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' },
    })
    if (outcome.status !== 'compiled') throw new Error('expected a report-only fixture')
    const draft = outcome.draft
    const run = await mockApi.createRun(draft)
    const [summary, rawSeries, activities] = await Promise.all([
      mockApi.getSummary(run.id), mockApi.getSeries(run.id), mockApi.getActivities(run.id),
    ])
    saveRecentBacktests('mock', [], [{
      id: run.id, draft, instrument: toUiInstrument(draft), strategy: toStrategySummary(draft),
      metrics: toBacktestMetrics(summary), evidence: toRunEvidence(summary), series: toSeries(rawSeries),
      marks: toChartMarks(toSeries(rawSeries), activities), trades: toTradeRows(activities), activities,
    }])
    const user = userEvent.setup()
    const compile = vi.spyOn(strategyApi, 'compile')
    const { container } = renderApp()
    const archived = container.querySelector<HTMLButtonElement>('[data-history-id]')!
    expect(archived).toHaveAttribute('data-history-id', run.id)
    await user.click(archived)
    expect(screen.getByText('只读历史')).toBeVisible()
    expect(detailControls().getByRole('heading', { name: '回测报告' })).toBeVisible()
    await user.click(detailControls().getByRole('button', { name: '回到对话' }))
    expect(container.querySelector('[id^="journey-"]')).toBeNull()
    expect(screen.queryByText('东方财富创20日新高且放量1.5倍买入，跌破20日线卖出', { selector: '.stream p' }))
      .not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: {
      value: '贵州茅台5日均线上穿20日均线买入，下穿卖出',
    } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(1))
    expect(compile.mock.calls[0]?.[0].relatedRunIds).toEqual([])
  }, 10_000)

  it('edits the completed current strategy into a new run while preserving the read-only previous report', async () => {
    // Component state regression, not real-model or real-data acceptance.
    settleMockRunOnFirstPoll()
    const createRun = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    const { container } = renderApp()
    fireEvent.change(screen.getByLabelText('交易规则'), { target: {
      value: '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出',
    } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText(/^查看(?:并修改|策略)$/)
    await user.click(await reviewControls().findByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    const originalTitle = container.querySelector('.detail-title')?.textContent
    expect(originalTitle).toBeTruthy()
    const originalDraft = createRun.mock.calls[0]?.[0]
    const originalRun = await createRun.mock.results[0]?.value
    if (!originalDraft || !originalRun) throw new Error('Expected the original completed run')
    expect(container.querySelectorAll('[data-current-conversation]')).toHaveLength(1)
    expect(container.querySelectorAll('[data-history-id]')).toHaveLength(0)

    await user.click(screen.getByRole('button', { name: '编辑策略' }))
    const review = within(screen.getByRole('complementary', { name: '策略审阅' }))
    expect(review.getByText('已完成回测')).toBeVisible()
    expect(review.queryByRole('button', { name: '开始回测' })).not.toBeInTheDocument()
    const settings = review.getByRole('button', { name: /高级设置/ })
    expect(settings).toBeEnabled()
    await user.click(settings)
    fireEvent.change(screen.getByRole('spinbutton', { name: '创 20 日新高 观察周期' }), {
      target: { value: '10' },
    })
    await user.click(screen.getByRole('button', { name: '完成' }))

    const historicalCards = container.querySelectorAll<HTMLElement>('.stream .mcard.is-settled')
    expect(historicalCards).toHaveLength(1)
    expect(historicalCards[0]).toHaveTextContent('创 20 日新高')
    for (const control of historicalCards[0]!.querySelectorAll('button')) expect(control).toBeDisabled()
    expect(screen.getByRole('button', { name: '查看这次报告' })).toBeEnabled()
    await user.click(await review.findByRole('button', { name: '开始回测' }))
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(2))
    const editedDraft = createRun.mock.calls[1]?.[0]
    const editedRun = await createRun.mock.results[1]?.value
    expect(editedRun.id).not.toBe(originalRun.id)
    expect(editedDraft?.entry.conditions[0]).toMatchObject({ parameters: [{ key: 'period', value: 10 }] })
    expect(originalDraft.entry.conditions[0]).toMatchObject({ parameters: [{ key: 'period', value: 20 }] })
    expect(editedDraft?.backtest).toEqual(originalDraft.backtest)
    expect(editedDraft?.exit).toEqual(originalDraft.exit)
    await screen.findByRole('heading', { name: '回测报告' })
    expect(container.querySelectorAll('[data-current-conversation]')).toHaveLength(1)
    expect(container.querySelectorAll('[data-history-id]')).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(screen.getByRole('button', { name: '查看这次报告' }))
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeVisible()
    expect(container.querySelector('.detail-title')?.textContent).toBe(originalTitle)
    await user.click(screen.getByRole('button', { name: '新建', hidden: true }))
    expect(container.querySelectorAll('[data-current-conversation]')).toHaveLength(0)
    expect(Array.from(container.querySelectorAll('[data-history-id]'))
      .map(item => item.getAttribute('data-history-id'))).toEqual([editedRun.id, originalRun.id])
    await user.click(container.querySelector(`[data-history-id="${editedRun.id}"]`)!)
    await user.click(detailControls().getByRole('button', { name: '回到对话' }))
    expect(container.querySelector(`#journey-${originalRun.id}`)).toBeTruthy()
    expect(container.querySelector(`#journey-${editedRun.id}`)).toBeTruthy()
    expect(screen.getByText('东方财富创20日新高且放量1.5倍买入，跌破20日线卖出', { selector: '.stream p' }))
      .toBeVisible()
  }, 20_000)

  it('keeps a completed run as a settled record when the user starts another rule', async () => {
    settleMockRunOnFirstPoll()
    const user = userEvent.setup()
    const { container } = renderApp()

    // Exercise conversation persistence, not per-keystroke input latency.
    fireEvent.change(screen.getByLabelText('交易规则'), { target: {
      value: '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出',
    } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText(/^查看(?:并修改|策略)$/)).toBeInTheDocument()
    const review = within(screen.getByRole('complementary', { name: '策略审阅' }))
    const chat = within(container.querySelector<HTMLElement>('#pg-chat')!)
    const report = () => within(container.querySelector<HTMLElement>('.detail')!)
    await user.click(await review.findByRole('button', { name: '开始回测' }))
    expect(
      await screen.findByText('回测结果', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()

    expect(chat.queryByRole('button', { name: /^换个条件$/ }))
      .not.toBeInTheDocument()
    await user.click(report().getByRole('button', { name: '回到对话' }))
    await user.click(review.getByRole('button', { name: '收起策略审阅' }))
    // jsdom does not apply the desktop media query that reveals the rail.
    await user.click(screen.getByRole('button', { name: '新建', hidden: true }))

    expect(screen.queryByText('历史回测结果')).not.toBeInTheDocument()
    expect(container.querySelectorAll('[data-current-conversation]')).toHaveLength(0)
    expect(container.querySelectorAll('[data-history-id]')).toHaveLength(1)
    expect(screen.getByLabelText('交易规则')).toHaveValue('')
    const archived = container.querySelector<HTMLButtonElement>('[data-history-id]')!
    await user.click(archived)
    expect(report().getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    const reportPage = container.querySelector('#pg-report') as HTMLElement
    expect(reportPage).not.toHaveTextContent('初始资金')
    expect(reportPage).not.toHaveTextContent('期末资产')
    expect(reportPage).toHaveTextContent('净值与回撤')
    expect(reportPage).toHaveTextContent('每笔委托')
    await user.click(report().getByRole('button', { name: '回到对话' }))

    const originalUtterance = '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出'
    expect(screen.getByText(originalUtterance, { selector: '.stream p' })).toBeVisible()
    expect(container.querySelector('[id^="journey-"]')).toBeTruthy()
    const input = screen.getByLabelText('交易规则')
    fireEvent.change(input, { target: { value: 'RSI 低于 30 买入，高于 70 卖出' } })
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect((await screen.findAllByText('RSI 低于 30')).length).toBeGreaterThan(0)
    const recentItems = container.querySelectorAll('.rail-list .rail-item')
    expect(recentItems).toHaveLength(1)
    expect(recentItems[0]).toHaveAttribute('data-current-conversation', 'true')
    expect(container.querySelector('#pg-chat')).not.toHaveTextContent(/本金|初始资金|100\s*万|1,000,000/)
  }, 10_000)
})
