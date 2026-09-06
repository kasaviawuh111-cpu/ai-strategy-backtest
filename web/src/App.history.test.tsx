import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, vi } from 'vitest'

import App from './App'
import { backtestApi } from './shared/api/client'
import { resetMockWaitForTests } from './shared/api/mock'
import { settleMockRunOnFirstPoll } from './test/mock-run'

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

afterEach(() => {
  vi.restoreAllMocks()
  resetMockWaitForTests()
})

describe('append-only strategy conversation', () => {
  it('edits the completed current strategy into a new run while preserving the read-only previous report', async () => {
    // Component state regression, not real-model or real-data acceptance.
    settleMockRunOnFirstPoll()
    const createRun = vi.spyOn(backtestApi, 'create')
    const user = userEvent.setup()
    const { container } = renderApp()
    await user.type(screen.getByLabelText('交易规则'),
      '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    await screen.findByText('预览策略')
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    await screen.findByRole('heading', { name: '回测报告' })
    const originalDraft = createRun.mock.calls[0]?.[0]
    const originalRun = await createRun.mock.results[0]?.value
    if (!originalDraft || !originalRun) throw new Error('Expected the original completed run')

    await user.click(screen.getByRole('button', { name: '编辑策略' }))
    const review = within(screen.getByRole('complementary', { name: '策略审阅' }))
    expect(review.getByText('已完成回测')).toBeVisible()
    expect(review.queryByRole('button', { name: '开始回测' })).not.toBeInTheDocument()
    const settings = review.getByRole('button', { name: /成交设置/ })
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
    await user.click(review.getByRole('button', { name: '开始回测' }))
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(2))
    const editedDraft = createRun.mock.calls[1]?.[0]
    const editedRun = await createRun.mock.results[1]?.value
    expect(editedRun.id).not.toBe(originalRun.id)
    expect(editedDraft?.entry.conditions[0]).toMatchObject({ parameters: [{ key: 'period', value: 10 }] })
    expect(originalDraft.entry.conditions[0]).toMatchObject({ parameters: [{ key: 'period', value: 20 }] })
    expect(editedDraft?.backtest).toEqual(originalDraft.backtest)
    expect(editedDraft?.exit).toEqual(originalDraft.exit)
    await screen.findByRole('heading', { name: '回测报告' })
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(screen.getByRole('button', { name: '查看这次报告' }))
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeVisible()
    expect(container.querySelector('.detail-title')).toHaveTextContent('创 20 日新高')
  })

  it('keeps a completed run as a settled record when the user starts another rule', async () => {
    settleMockRunOnFirstPoll()
    const user = userEvent.setup()
    const { container } = renderApp()

    await user.type(screen.getByRole('textbox', { name: '交易规则' }),
      '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('预览策略')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(
      await screen.findByText('回测结果', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()

    expect(screen.queryByRole('button', { name: /^换个条件$/ }))
      .not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '回到对话' }))
    await user.click(screen.getByRole('button', { name: '收起策略审阅' }))
    // jsdom does not apply the desktop media query that reveals the rail.
    await user.click(screen.getByRole('button', { name: '新建策略', hidden: true }))

    expect(screen.getByText('历史回测结果')).toBeInTheDocument()
    expect(screen.getByText('策略与回测摘要')).toBeInTheDocument()
    expect(screen.getByText('说出新的买卖规则，继续回测。'))
      .toBeInTheDocument()
    expect(screen.getAllByText('已完成回测').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: '查看这次报告' })).toBeEnabled()
    for (const control of container.querySelectorAll<HTMLButtonElement>('.is-settled button')) {
      expect(control).toBeDisabled()
    }

    await user.click(screen.getByRole('button', { name: '查看这次报告' }))
    expect(screen.getByRole('heading', { name: '回测报告' })).toBeInTheDocument()
    const reportPage = container.querySelector('#pg-report') as HTMLElement
    expect(reportPage).not.toHaveTextContent('初始资金')
    expect(reportPage).not.toHaveTextContent('期末资产')
    expect(reportPage).toHaveTextContent('净值与回撤')
    expect(reportPage).toHaveTextContent('每笔委托')
    await user.click(screen.getByRole('button', { name: '回到对话' }))

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, 'RSI 低于 30 买入，高于 70 卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect((await screen.findAllByText('RSI 低于 30')).length).toBeGreaterThan(0)
    expect(screen.getByText('历史回测结果')).toBeInTheDocument()
    expect(container.querySelector('#pg-chat')).not.toHaveTextContent(/本金|初始资金|100\s*万|1,000,000/)
  }, 10_000)
})
