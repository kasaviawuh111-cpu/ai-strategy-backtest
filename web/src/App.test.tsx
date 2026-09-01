import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, vi } from 'vitest'

import App from './App'
import { strategyApi } from './shared/api/client'
import { resetMockWaitForTests } from './shared/api/mock'
import { ApiError, type Instrument } from './shared/api/types'
import { settleMockRunOnFirstPoll } from './test/mock-run'

const renderApp = (
  instrument?: Instrument,
  options?: {
    instrumentContextSource?: 'stock_page' | 'standalone_default'
    instrumentContextError?: string
    onReturnToStockPage?: () => void
    onUseStandaloneExample?: () => void
  },
) => {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return {
    client,
    ...render(
      <QueryClientProvider client={client}>
        <App instrument={instrument} {...options} />
      </QueryClientProvider>,
    ),
  }
}

const expectMockPreviewBadge = () => {
  expect(document.querySelector('.app')).toHaveAttribute('data-api-mode', 'mock')
  expect(document.querySelector('.mode-pill')).toHaveTextContent('界面预览')
  expect(document.querySelector('.mode-pill')).toBeVisible()
}

const HOME_CAPITAL_PATTERN = /(?:本金|初始资金|起始本金|100\s*万|1,000,000|1000000)/
const VOLUME_EXAMPLE = '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出'
const FINANCIAL_EXAMPLE = '东方财富PE低于35且MACD金叉买入，MACD死叉卖出'

const expectHomeToHideDefaultCapital = (container: HTMLElement) => {
  const home = container.querySelector<HTMLElement>('#pg-chat')
  if (!home) throw new Error('missing #pg-chat')

  const formValues = Array.from(
    home.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement>(
      'input, textarea, select',
    ),
    (control) => control.value,
  )
  const visibleSurface = [home.textContent ?? '', ...formValues].join('\n')

  expect(home).toBeVisible()
  expect(visibleSurface).not.toMatch(HOME_CAPITAL_PATTERN)
}

afterEach(() => {
  vi.restoreAllMocks()
  resetMockWaitForTests()
})

describe('formal main.tsx App journey', () => {
  it('does not visibly fall back to 东方财富 when the stock-page symbol is invalid', async () => {
    const onReturnToStockPage = vi.fn()
    const onUseStandaloneExample = vi.fn()
    const user = userEvent.setup()
    renderApp(undefined, {
      instrumentContextError: '股票页没有提供有效的 A 股代码。',
      onReturnToStockPage,
      onUseStandaloneExample,
    })

    expect(screen.getByText(/未能识别当前股票/)).toBeInTheDocument()
    expect(screen.getByText('股票页上下文无效')).toBeInTheDocument()
    expect(screen.queryByText(/说出买卖规则/)).not.toBeInTheDocument()
    expect(screen.getByLabelText('交易规则')).toHaveValue('')
    expect(screen.queryByLabelText('策略示例')).not.toBeInTheDocument()

    const returnButtons = screen.getAllByRole('button', { name: '返回股票页' })
    await user.click(returnButtons.at(-1) as HTMLButtonElement)
    expect(onReturnToStockPage).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: '使用东方财富示例' }))
    expect(onUseStandaloneExample).toHaveBeenCalledTimes(1)
  })

  it('uses the A-share instrument supplied by the host stock page', async () => {
    const compile = vi.spyOn(strategyApi, 'compile')
    const user = userEvent.setup()
    const instrument: Instrument = {
      name: '贵州茅台', symbol: '600519.SH', market: 'CN_A', exchange: 'SSE',
    }
    renderApp(instrument)

    expect(screen.getByText('想怎么交易？用一句话告诉我，我来帮你把它变成可回测的策略。').closest('.say'))
      .toBeInTheDocument()
    expect(screen.getByLabelText('交易规则')).toHaveValue('')
    expect(screen.getByRole('button', {
      name: '贵州茅台创20日新高且放量1.5倍买入，跌破20日线卖出',
    })).toBeVisible()
    await user.click(screen.getByRole('button', {
      name: '贵州茅台创20日新高且放量1.5倍买入，跌破20日线卖出',
    }))
    const thinking = screen.getByRole('status', { name: '思考进度' })
    expect(thinking).toHaveTextContent('正在识别买入、卖出和回测区间')
    expect(thinking.closest('.thinking-stream')).not.toHaveClass('mcard')
    expect(await screen.findByText('已完成思考')).toBeInTheDocument()
    const completedThinking = screen.getByRole('button', { name: /已完成思考/ })
    expect(completedThinking.querySelector('b')).toBeNull()
    expect(completedThinking.querySelector('.think-title')).toHaveTextContent('已完成思考')
    expect(completedThinking).toHaveAttribute('aria-expanded', 'false')
    await user.click(completedThinking)
    expect(completedThinking).toHaveAttribute('aria-expanded', 'true')
    expect(compile).toHaveBeenCalledWith(expect.objectContaining({ instrument }))
  })

  it('keeps the Mock identity visible and never labels Mock evidence as proved', async () => {
    const user = userEvent.setup()
    const { container } = renderApp()

    expect(container.querySelector('.app')).toHaveAttribute('data-api-mode', 'mock')
    expectMockPreviewBadge()
    expect(screen.queryByText(/^proved$/i)).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: VOLUME_EXAMPLE }))
    expect(await screen.findByText('已完成思考')).toBeInTheDocument()
    expectMockPreviewBadge()
    expect(screen.queryByText(/^proved$/i)).not.toBeInTheDocument()
  })

  it('only shows the return-to-bottom control after the conversation is scrolled away', () => {
    const { container } = renderApp()
    const scroll = container.querySelector<HTMLElement>('#pg-chat > .scroll')
    if (!scroll) throw new Error('missing chat scroll container')

    Object.defineProperties(scroll, {
      scrollHeight: { configurable: true, value: 1_000 },
      clientHeight: { configurable: true, value: 400 },
      scrollTop: { configurable: true, value: 100, writable: true },
    })

    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
    fireEvent.scroll(scroll)
    expect(screen.getByRole('button', { name: '回到底部' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: '回到底部' }))
    expect(screen.queryByRole('button', { name: '回到底部' })).not.toBeInTheDocument()
  })

  it('runs the current technical journey through settings, report, drawdown, and causal trace', async () => {
    settleMockRunOnFirstPoll({ preserveFirstRunningState: true })
    const user = userEvent.setup()
    const { container } = renderApp()

    expect(screen.getByLabelText('交易规则')).toHaveValue('')
    const examples = within(screen.getByLabelText('策略示例'))
    expect(examples.getAllByRole('button')).toHaveLength(2)
    expect(examples.getByRole('button', { name: VOLUME_EXAMPLE })).toBeVisible()
    expect(examples.getByRole('button', { name: FINANCIAL_EXAMPLE })).toBeVisible()
    expectHomeToHideDefaultCapital(container)
    await user.click(examples.getByRole('button', { name: VOLUME_EXAMPLE }))
    expect(await screen.findByText('已完成思考')).toBeInTheDocument()
    expect(screen.getAllByText('创 20 日新高').length).toBeGreaterThan(0)
    expect(screen.getAllByText('放量 1.5 倍').length).toBeGreaterThan(0)
    expect(screen.getAllByText('收盘跌破 20 日均线').length).toBeGreaterThan(0)
    expectHomeToHideDefaultCapital(container)

    await user.click(screen.getByRole('button', { name: /区间/ }))
    expect(screen.getByRole('heading', { name: '策略设置' })).toBeInTheDocument()
    const initialCash = screen.getByRole('spinbutton', { name: '初始资金' })
    expect(initialCash).toHaveValue(1000000)
    await user.clear(initialCash)
    await user.type(initialCash, '500000')
    await user.click(screen.getByRole('button', { name: '完成' }))
    expectHomeToHideDefaultCapital(container)

    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(await screen.findByText(/^预览：/)).toBeInTheDocument()
    expect(screen.queryByText(/后台返回的处理阶段/)).not.toBeInTheDocument()
    expect(
      await screen.findByText('回测结果', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()
    expectMockPreviewBadge()
    expectHomeToHideDefaultCapital(container)
    expect(screen.getByText(
      '策略亏损 2.54%，同样的钱买入后一直持有亏损 39.23%，相对少亏 36.69 个百分点。',
    )).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^换个条件$/ }))
      .not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '查看完整报告' }))
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    expect(document.querySelector('#pg-report .report-summary-meta .tag')).toBeNull()
    expect(screen.queryByText(/^proved$/i)).not.toBeInTheDocument()
    expect(document.querySelectorAll('#pg-report .report-risk .notice')).toHaveLength(0)
    expect(within(document.querySelector('#pg-report') as HTMLElement)
      .queryByText(/风险提示/)).not.toBeInTheDocument()
    const reportPage = within(document.querySelector('#pg-report') as HTMLElement)
    expect(reportPage.queryByText('初始资金')).not.toBeInTheDocument()
    expect(reportPage.queryByText('期末资产')).not.toBeInTheDocument()
    expect(reportPage.queryByRole('tablist')).not.toBeInTheDocument()
    expect(reportPage.getByRole('heading', { name: '净值与回撤' })).toBeInTheDocument()
    expect(reportPage.getByRole('heading', { name: '每笔委托' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '返回' }))
    expectHomeToHideDefaultCapital(container)
    await user.click(screen.getByRole('button', { name: '查看完整报告' }))
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    expect(screen.getAllByText('最大回撤').length).toBeGreaterThan(0)
    // 点位明细已经并进「每笔委托」列表，图表下面不再有第二套入口。
    expect(screen.queryByRole('button', { name: '选择点位' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '每笔委托' })).toBeInTheDocument()
    for (const label of ['方向 / 时间', '信号', '委托价', '状态 / 成交价']) {
      expect(screen.getAllByText(label, { exact: true }).length).toBeGreaterThan(0)
    }

    await user.click(screen.getByRole('button', { name: /买入 MACD 金叉确认 未成/ }))
    expect(screen.getByText('从你那句话到账户变化')).toBeInTheDocument()
    expect(screen.getByText('用户原话')).toBeInTheDocument()
    expect(screen.getByText('规范化条件')).toBeInTheDocument()
    for (const label of ['MACD 金叉确认', '形成买入决策', '提交买入委托', '涨停未成交', '账户净值记录']) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0)
    }
    const chainPage = within(document.querySelector('#pg-chain') as HTMLElement)
    expect(chainPage.getByText('技术详情')).toBeVisible()
    for (const identity of chainPage.getAllByText(/chain decision_buy_blocked/)) {
      expect(identity).not.toBeVisible()
    }
    await user.click(chainPage.getByText('技术详情'))
    for (const identity of chainPage.getAllByText(/chain decision_buy_blocked/)) {
      expect(identity).toBeVisible()
    }
  }, 12_000)

  it('keeps a complete result authoritative when a later run-status refresh returns 404', async () => {
    const getRun = settleMockRunOnFirstPoll()
    const user = userEvent.setup()
    const { client } = renderApp()

    await user.click(screen.getByRole('button', { name: VOLUME_EXAMPLE }))
    expect(await screen.findByText('已完成思考')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(await screen.findByText('回测结果', {}, { timeout: 6_000 })).toBeInTheDocument()

    getRun.mockRejectedValueOnce(new ApiError({
      type: 'about:blank',
      title: '请求未完成',
      status: 404,
      detail: 'Backtest run was not found',
      code: 'backtest_run_not_found',
    }))
    await client.refetchQueries({ queryKey: ['backtest-run'] })

    expect(screen.getByText('回测结果')).toBeInTheDocument()
    expect(screen.queryByText('任务状态读取失败')).not.toBeInTheDocument()
    expect(screen.queryByText('Backtest run was not found')).not.toBeInTheDocument()
  }, 8_000)

  it('runs the volume-breakout example as two technical entry conditions', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.click(screen.getByRole('button', { name: VOLUME_EXAMPLE }))
    expect(await screen.findByText('已完成思考')).toBeInTheDocument()
    expect(screen.getAllByText('创 20 日新高').length).toBeGreaterThan(0)
    expect(screen.getAllByText('放量 1.5 倍').length).toBeGreaterThan(0)
    expect(screen.getAllByText('收盘跌破 20 日均线').length).toBeGreaterThan(0)
    expect(screen.queryByText('业绩预告发布')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(
      await screen.findByText('回测结果', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '查看完整报告' }))
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    expect(screen.queryByText(/^proved$/i)).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '净值与回撤' })).toBeInTheDocument()
  }, 9_000)

  it('shows the exact report-text threshold and fill-anchored trading-session exit as Mock', async () => {
    settleMockRunOnFirstPoll()
    const user = userEvent.setup()
    renderApp({ name: '同花顺', symbol: '300033.SZ', market: 'CN_A', exchange: 'SZSE' })

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '同花顺发年报提到ai次数超过5次的话就买入，3天后卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText('已完成思考')).toBeInTheDocument()
    expect(screen.getAllByText('年度报告正文中“AI”完整词出现 > 5 次').length).toBeGreaterThan(0)
    expect(screen.getAllByText('实际买入成交后第 3 个交易日卖出').length).toBeGreaterThan(0)
    expect(screen.getByText(/没有读取年报正文，也没有计算词频/)).toBeInTheDocument()
    expectMockPreviewBadge()

    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(
      await screen.findByText('回测结果', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '查看完整报告' }))
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '每笔委托' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', {
      name: /买入 年度报告正文词频条件确认 已成/,
    }))

    expect(screen.getByRole('heading', { name: /买入成交 · 因果轨迹/ })).toBeInTheDocument()
    const chainPage = within(document.querySelector('#pg-chain') as HTMLElement)
    expect(chainPage.getByText('从你那句话到账户变化')).toBeInTheDocument()
    expect(chainPage.getByText('用户原话')).toBeInTheDocument()
    expect(chainPage.getByText('规范化条件')).toBeInTheDocument()
    expect(chainPage.getByText('同花顺发年报提到ai次数超过5次的话就买入，3天后卖出')).toBeInTheDocument()
    expect(chainPage.getByText(/买入：年度报告正文中“AI”完整词出现 > 5 次/)).toBeInTheDocument()
    expect(chainPage.getByText(/卖出：实际买入成交后第 3 个交易日卖出/)).toBeInTheDocument()
    expect(chainPage.getByText(/^成交时间质量：$/)).toBeInTheDocument()
    expect(chainPage.getByText(/不代表已观测到该时刻的真实成交/)).toBeInTheDocument()
    expectMockPreviewBadge()
  }, 10_000)

  it('keeps recognized stock, annual report, term threshold, and holding period in one clarification', async () => {
    const original = '同花顺发年报提到ai次数超过5次的话就买入，3天后卖出'
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_low_confidence',
      clarification: {
        id: 'candidate_provider_low_confidence',
        question: '这条规则里有一个低置信度片段，请补充后再继续。',
        reason: '系统只集中确认一次，不会丢掉已经识别的条件。',
        recognized: [
          { label: '股票', value: '同花顺 300033.SZ' },
          { label: '公告事件', value: '年度报告' },
          { label: '正文条件', value: 'AI 完整词出现 > 5 次' },
          { label: '卖出', value: '实际买入成交后持有 3 个交易日' },
        ],
        choices: [{
          id: 'edit-utterance',
          label: '补充这一个片段',
          description: '返回原话继续补充；已识别内容保持不变。',
          recommended: true,
          action: 'edit_utterance',
        }],
      },
    })
    const user = userEvent.setup()
    renderApp({ name: '同花顺', symbol: '300033.SZ', market: 'CN_A', exchange: 'SZSE' })

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, original)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText('只问这一次')).toBeInTheDocument()
    for (const preserved of [
      '股票：同花顺 300033.SZ',
      '公告事件：年度报告',
      '正文条件：AI 完整词出现 > 5 次',
      '卖出：实际买入成交后持有 3 个交易日',
    ]) {
      expect(screen.getByText(preserved)).toBeInTheDocument()
    }
    expect(screen.queryByText('无法识别这条策略')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /补充这一个片段/ }))
    expect(screen.getByLabelText('交易规则')).toHaveValue(original)
    expect(screen.getByLabelText('交易规则')).toHaveFocus()
    expect(compile).toHaveBeenCalledTimes(1)
  })

  it('asks for a missing standalone stock in text and merges the typed reply with the saved rule', async () => {
    const original = 'MACD金叉买入，MACD死叉卖出，回测近1年'
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_missing_instrument',
      clarification: {
        id: 'instrument_required',
        question: '请补充股票名称或 6 位证券代码，我会继续沿用刚才的买卖规则。',
        reason: '买卖条件已经保留，现在只缺回测标的。',
        choices: [],
      },
    })
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.type(input, original)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText(
      '请补充股票名称或 6 位证券代码，我会继续沿用刚才的买卖规则。',
    )).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '补充股票代码' })).not.toBeInTheDocument()
    expect(input).toBeEnabled()
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', '输入股票名称或 6 位代码')
    await waitFor(() => expect(input).toHaveFocus())

    await user.type(input, '同花顺')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    await waitFor(() => expect(compile).toHaveBeenCalledTimes(2))
    expect(compile).toHaveBeenLastCalledWith(expect.objectContaining({
      utterance: `同花顺，${original}`,
      instrumentContextSource: 'standalone_default',
    }))
  })

  it('offers a trusted stock-page instrument while keeping free stock input available', async () => {
    const instrument: Instrument = {
      name: '贵州茅台', symbol: '600519.SH', market: 'CN_A', exchange: 'SSE',
    }
    vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_stock_page_instrument',
      clarification: {
        id: 'instrument_required',
        question: '请确认使用当前股票“贵州茅台”，或在下方输入其他股票名称或 6 位证券代码。',
        reason: '买卖条件已经保留，现在只缺回测标的。',
        choices: [{
          id: 'use-current-instrument',
          label: '使用 贵州茅台',
          description: '继续回测 600519.SH。',
          recommended: true,
          action: 'submit_clarification',
        }],
      },
    })
    const user = userEvent.setup()
    renderApp(instrument, { instrumentContextSource: 'stock_page' })

    const input = screen.getByLabelText('交易规则')
    await user.type(input, 'MACD金叉买入，MACD死叉卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByRole('button', { name: /使用 贵州茅台/ })).toBeEnabled()
    expect(screen.queryByRole('button', { name: '补充股票代码' })).not.toBeInTheDocument()
    expect(input).toBeEnabled()
    expect(input).toHaveAttribute('placeholder', '输入股票名称或 6 位代码')
    await waitFor(() => expect(input).toHaveFocus())
  })

  it('separates recognized document rules with missing data from unknown language', async () => {
    vi.spyOn(strategyApi, 'compile').mockRejectedValueOnce(new ApiError({
      type: 'about:blank',
      title: '报告正文数据不可用',
      status: 422,
      detail: '当前固定快照没有年度报告正文提取结果。',
      code: 'event_document_text_data_unavailable',
    }))
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '年报正文 AI 超过 5 次买入，3 个交易日后卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText('已理解规则，但缺少报告正文数据')).toBeInTheDocument()
    expect(screen.getByText(/不会用公告标题代替正文，也不会猜测词频/)).toBeInTheDocument()
    expect(screen.queryByText('无法识别这条策略')).not.toBeInTheDocument()
  })

  it('rejects an unsupported sentence instead of silently falling back to MACD', async () => {
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '火星逆行时满仓，月圆时卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText('无法识别这条策略')).toBeInTheDocument()
    expect(screen.getByText(/当前可执行的技术指标或公告事件/)).toHaveTextContent(
      '没有识别到当前可执行的技术指标或公告事件。请写清何时买入、何时卖出和回测区间，或先使用页面示例。',
    )
    expect(document.body).not.toHaveTextContent('no_supported_signal_recognized')
    expect(screen.queryByText('MACD 金叉')).not.toBeInTheDocument()
  })

  it('guides an unavailable previous-session limit-up rule through plain text input', async () => {
    const original = '东方财富昨天涨停今天买入，短线卖出'
    const compile = vi.spyOn(strategyApi, 'compile').mockRejectedValueOnce(new ApiError({
      type: 'about:blank',
      title: '前一交易日涨停能力不可用',
      status: 422,
      detail: '当前运行时尚未发布前一交易日涨停信号。',
      code: 'previous_session_limit_up_capability_unavailable',
    }))
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.type(input, original)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText(/我已理解你想用“前一交易日涨停”作为买入条件/))
      .toBeInTheDocument()
    expect(screen.getByText(/“短线”没有明确的卖出时点/)).toBeInTheDocument()
    expect(screen.queryByText('这句话暂时不能还原')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '修改规则' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '使用技术示例' })).not.toBeInTheDocument()
    expect(input).toBeEnabled()
    expect(input).toHaveValue('')
    expect(input).toHaveAttribute('placeholder', '补充卖出方式，或换一种买入条件')
    await waitFor(() => expect(input).toHaveFocus())
    expect(screen.getByText(original)).toBeInTheDocument()

    const replacement = '东方财富MACD金叉买入，MACD死叉卖出'
    await user.type(input, replacement)
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await waitFor(() => expect(compile).toHaveBeenCalledTimes(2))
    expect(compile).toHaveBeenLastCalledWith(expect.objectContaining({ utterance: replacement }))
  })

  it('guides an opinion into explicit A-share strategy choices before allowing a backtest', async () => {
    const originalCompile = strategyApi.compile
    const compile = vi.spyOn(strategyApi, 'compile')
      .mockResolvedValueOnce({
        status: 'needs_clarification',
        draftId: 'idea-route-draft',
        clarification: {
          id: 'idea_guidance_required',
          question: '选一个方向，我会把它变成完整买卖规则再识别。',
          reason: '你在表达对特朗普相关政策的不认同，但还没有给出可回测的买卖条件。',
          ideaRoute: {
            schema_version: 'idea-route.v1',
            understanding: '你在表达对特朗普相关政策的不认同。',
            hypothesis: '先把观点转换成当前股票可检验的价格代理。',
            asset_mapping: {
              instrument_symbol: '300059.SZ',
              relation: 'current_page_proxy',
              rationale: '当前页面是东方财富，只围绕这只 A 股提出候选。',
              evidence_status: 'host_context_only',
            },
            proposals: [{
              id: 'trend-confirmation',
              title: '等趋势确认',
              hypothesis: '价格与趋势同时转强后再进入。',
              entry_summary: 'MACD 金叉且站上 20 日均线',
              exit_summary: 'MACD 死叉',
              suggested_utterance: 'MACD 金叉且站上 20 日均线买入，MACD 死叉卖出，回测近 5 年',
              capability_ids: ['technical.macd', 'technical.ma'],
              assumptions: ['仅使用价格代理'],
              confidence: 0.82,
            }, {
              id: 'oversold-rebound',
              title: '等超跌反弹',
              hypothesis: '仅在超跌后恢复时进入。',
              entry_summary: 'RSI 低于 30',
              exit_summary: 'RSI 高于 70',
              suggested_utterance: 'RSI 低于 30 买入，RSI 高于 70 卖出，回测近 5 年',
              capability_ids: ['technical.rsi'],
              assumptions: ['仅使用价格代理'],
              confidence: 0.75,
            }],
          },
          choices: [{
            id: 'trend-confirmation',
            label: '等趋势确认',
            description: '价格与趋势同时转强后再进入。',
            action: 'replace_and_compile',
            suggestedUtterance: 'MACD 金叉且站上 20 日均线买入，MACD 死叉卖出，回测近 5 年',
          }, {
            id: 'oversold-rebound',
            label: '等超跌反弹',
            description: '仅在超跌后恢复时进入。',
            action: 'replace_and_compile',
            suggestedUtterance: 'RSI 低于 30 买入，RSI 高于 70 卖出，回测近 5 年',
          }],
        },
      })
      .mockImplementationOnce((input) => originalCompile(input))
    const revise = vi.spyOn(strategyApi, 'revise')
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '我讨厌特朗普')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByRole('button', { name: /等趋势确认/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /等超跌反弹/ })).toBeInTheDocument()
    expect(screen.getByText('你在表达对特朗普相关政策的不认同，但还没有给出可回测的买卖条件。')).toBeInTheDocument()
    expect(screen.getByText('价格与趋势同时转强后再进入。')).toBeInTheDocument()
    expect(screen.getByText('仅在超跌后恢复时进入。')).toBeInTheDocument()
    expect(screen.getByText('2 条可选规则')).toBeInTheDocument()
    expect(screen.getByPlaceholderText('也可以直接打字告诉我')).toBeInTheDocument()
    expect(screen.queryByText('已理解观点：')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '开始回测' })).not.toBeInTheDocument()
    expect(compile).toHaveBeenCalledTimes(1)
    expect(revise).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /等趋势确认/ }))

    expect(input).toHaveValue('MACD 金叉且站上 20 日均线买入，MACD 死叉卖出，回测近 5 年')
    expect(compile).toHaveBeenCalledTimes(2)
    expect(compile).toHaveBeenNthCalledWith(2, expect.objectContaining({
      utterance: 'MACD 金叉且站上 20 日均线买入，MACD 死叉卖出，回测近 5 年',
      clarification: undefined,
    }))
    expect(await screen.findByRole('button', { name: '开始回测' })).toBeInTheDocument()
    expect(revise).not.toHaveBeenCalled()
  })

  it('keeps the server-confirmed instrument when compiling an idea-route choice', async () => {
    const originalCompile = strategyApi.compile
    const compile = vi.spyOn(strategyApi, 'compile')
      .mockResolvedValueOnce({
        status: 'needs_clarification',
        draftId: 'ambiguous-cross-draft',
        clarification: {
          id: 'ambiguous_cross_indicator',
          question: '“金叉/死叉”指的是哪一类指标？',
          reason: '已确认标的为汤姆猫，但金叉可能指多种指标。',
          ideaRoute: {
            schema_version: 'idea-route.v1',
            understanding: '你想在汤姆猫出现金叉时买入，死叉时卖出。',
            hypothesis: '先确认金叉所属指标。',
            asset_mapping: {
              instrument_symbol: '300459.SZ',
              relation: 'current_page_proxy',
              rationale: '服务端证券主数据已确认汤姆猫。',
              evidence_status: 'host_context_only',
            },
            proposals: [],
          },
          choices: [{
            id: 'macd-cross',
            label: 'MACD 金叉 / 死叉',
            description: 'MACD 金叉买入，MACD 死叉卖出',
            action: 'replace_and_compile',
            suggestedUtterance: 'MACD金叉买入，MACD死叉卖出，回测近1年',
            instrumentSymbol: '300459.SZ',
            instrumentName: '汤姆猫',
          }, {
            id: 'kdj-cross',
            label: 'KDJ 金叉 / 死叉',
            description: 'KDJ 金叉买入，KDJ 死叉卖出',
            action: 'replace_and_compile',
            suggestedUtterance: 'KDJ金叉买入，KDJ死叉卖出，回测近1年',
            instrumentSymbol: '300459.SZ',
            instrumentName: '汤姆猫',
          }],
        },
      })
      .mockImplementationOnce((input) => originalCompile(input))
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '汤姆猫金叉买死叉卖')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await user.click(await screen.findByRole('button', { name: /MACD 金叉/ }))

    expect(compile).toHaveBeenNthCalledWith(2, expect.objectContaining({
      utterance: 'MACD金叉买入，MACD死叉卖出，回测近1年',
      instrument: expect.objectContaining({
        name: '汤姆猫',
        symbol: '300459.SZ',
        exchange: 'SZSE',
      }),
      instrumentContextSource: 'stock_page',
    }))
    expect(await screen.findByText('汤姆猫')).toBeInTheDocument()
  })

  it('does not present uncovered catalog events as runnable event strategies', async () => {
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '东方财富最终中标后买入，MACD 死叉卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText('无法识别这条策略')).toBeInTheDocument()
    expect(screen.getByText(/当前可执行的技术指标或公告事件/)).toBeInTheDocument()
    expect(document.body).not.toHaveTextContent('no_supported_signal_recognized')
    expect(screen.queryByText('最终中标')).not.toBeInTheDocument()
  })

  it('returns a bare indicator name to the input instead of inventing a strategy', async () => {
    const compile = vi.spyOn(strategyApi, 'compile')
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, 'MACD')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('只问这一次')).toBeInTheDocument()
    expect(screen.getByText(/系统不会替你补默认买卖规则/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /补充完整规则/ }))
    expect(screen.getByLabelText('交易规则')).toHaveValue('MACD')
    expect(screen.getByLabelText('交易规则')).toHaveFocus()
    expect(compile).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/用到 \d+ 个条件/)).not.toBeInTheDocument()
  })

  it('returns a missing-exit clarification to the original input without inventing a rule', async () => {
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_missing_exit',
      clarification: {
        id: 'exit_rule_not_recognized',
        question: '已识别买入条件。你想在什么条件下卖出？',
        reason: '卖出条件决定何时结束持仓。系统不会替你补一条默认策略。',
        choices: [{
          id: 'edit-utterance',
          label: '补充卖出条件',
          description: '返回输入框，在原话后补充明确的卖出条件。',
          recommended: true,
          action: 'edit_utterance',
        }],
      },
    })
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, '年度报告发布后买入')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText('只问这一次')).toBeInTheDocument()
    expect(screen.getByText(/系统不会替你补一条默认策略/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /使用东方财富/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /补充卖出条件/ }))

    expect(screen.getByLabelText('交易规则')).toHaveValue('年度报告发布后买入')
    expect(screen.getByLabelText('交易规则')).toHaveFocus()
    expect(screen.queryByText('只问这一次')).not.toBeInTheDocument()
    expect(compile).toHaveBeenCalledTimes(1)
    expect(screen.queryByText(/用到 \d+ 个条件/)).not.toBeInTheDocument()
  })

  it('returns a missing-entry clarification with the correct buy direction', async () => {
    const compile = vi.spyOn(strategyApi, 'compile').mockResolvedValueOnce({
      status: 'needs_clarification',
      draftId: 'draft_missing_entry',
      clarification: {
        id: 'entry_rule_not_recognized',
        question: '已识别卖出条件。你想在什么条件下买入？',
        reason: '买入条件决定什么时候建立持仓。系统不会替你补一条默认策略。',
        choices: [{
          id: 'edit-utterance',
          label: '补充买入条件',
          description: '返回输入框，在原话前补充明确的买入条件。',
          recommended: true,
          action: 'edit_utterance',
        }],
      },
    })
    const user = userEvent.setup()
    renderApp()

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, 'MACD死叉卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))

    expect(await screen.findByText('只问这一次')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /补充买入条件/ })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /补充卖出条件/ })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /补充买入条件/ }))
    expect(screen.getByLabelText('交易规则')).toHaveValue('MACD死叉卖出')
    expect(screen.getByLabelText('交易规则')).toHaveFocus()
    expect(compile).toHaveBeenCalledTimes(1)
  })

  it('runs the financial example without dropping its PE condition', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.click(screen.getByRole('button', { name: FINANCIAL_EXAMPLE }))
    expect(await screen.findByText('已完成思考')).toBeInTheDocument()
    expect(screen.getAllByText('市盈率 < 35').length).toBeGreaterThan(0)
    expect(screen.getAllByText('MACD 金叉').length).toBeGreaterThan(0)
    expect(screen.getAllByText('MACD 死叉').length).toBeGreaterThan(0)
  })

  it('exposes a user-cancelled run as an actionable terminal state', async () => {
    const user = userEvent.setup()
    renderApp()

    await user.click(screen.getByRole('button', { name: VOLUME_EXAMPLE }))
    expect(await screen.findByText('已完成思考')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(await screen.findByText(/^预览：/)).toBeInTheDocument()
    const runProgress = screen.getByRole('status', { name: '回测进度' })
    expect(runProgress.closest('.thinking-stream')).not.toHaveClass('mcard')
    expect(runProgress.closest('.thinking-stream')).toHaveTextContent('运行这次历史回测')
    await user.click(await screen.findByRole(
      'button',
      { name: '取消回测' },
      { timeout: 4_000 },
    ))
    expect(await screen.findByText('用户取消')).toBeInTheDocument()
    expect(screen.getByText(/任务已停止/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '修改规则' })).toBeEnabled()
  })

  it('restores the prototype follow-up chips without surfacing capital on the home journey', async () => {
    settleMockRunOnFirstPoll()
    const user = userEvent.setup()
    const { container } = renderApp()

    expectHomeToHideDefaultCapital(container)
    await user.click(screen.getByRole('button', { name: VOLUME_EXAMPLE }))
    expect(await screen.findByText('已完成思考')).toBeInTheDocument()
    expectHomeToHideDefaultCapital(container)
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(
      await screen.findByText('回测结果', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()
    expectHomeToHideDefaultCapital(container)

    expect(screen.queryByText('接下来可以继续验证')).not.toBeInTheDocument()
    expect(screen.getByRole('group', { name: '可选的下一步' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '换个条件再跑一次' })).toBeEnabled()
    const reminder = screen.getByRole('button', { name: '把这条设成盯盘提醒' })
    expect(reminder).toHaveAttribute('title', '当前只记录在本页，尚未连接通知服务')
    await user.click(reminder)
    expect(screen.getByRole('button', { name: '已设置盯盘提醒' })).toBeDisabled()
    const reminderStatus = screen.getByRole('status')
    expect(reminderStatus).toHaveTextContent('盯盘提醒已设置')
    expect(reminderStatus).toHaveTextContent('当前只记录在本页，尚未连接通知服务')
    expect(screen.getByRole('button', { name: '换只股票试试' })).toHaveAttribute(
      'title',
      '功能入口，暂未接入股票切换',
    )
    await user.click(screen.getByRole('button', { name: '查看完整报告' }))
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '净值与回撤' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: '每笔委托' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '返回' }))
    expectHomeToHideDefaultCapital(container)

    await user.click(screen.getByRole('button', { name: '换个条件再跑一次' }))
    expect(screen.getByLabelText('交易规则')).toHaveFocus()
    expect(screen.queryByText('盯盘提醒已设置')).not.toBeInTheDocument()
    expectHomeToHideDefaultCapital(container)
  }, 10_000)
})
