import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, vi } from 'vitest'

import App from './App'
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
  it('keeps a completed run as a settled record when the user starts another rule', async () => {
    settleMockRunOnFirstPoll()
    const user = userEvent.setup()
    const { container } = renderApp()

    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect(await screen.findByText('已读懂你的规则')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '开始回测' }))
    expect(
      await screen.findByText('回测完成。', {}, { timeout: 6_000 }),
    ).toBeInTheDocument()

    expect(screen.queryByRole('button', { name: /^换个条件$/ }))
      .not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '换个条件再跑' }))

    expect(screen.getByText('历史回测结果')).toBeInTheDocument()
    expect(screen.getByText('上一次的结果保留在上面。直接说新的买卖规则，我接着往下还原。'))
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
    await user.click(screen.getByRole('button', { name: '返回' }))

    const input = screen.getByLabelText('交易规则')
    await user.clear(input)
    await user.type(input, 'RSI 低于 30 买入，高于 70 卖出')
    await user.click(screen.getByRole('button', { name: '识别交易规则' }))
    expect((await screen.findAllByText('RSI 低于 30')).length).toBeGreaterThan(0)
    expect(screen.getByText('历史回测结果')).toBeInTheDocument()
    expect(container.querySelector('#pg-chat')).not.toHaveTextContent(/本金|初始资金|100\s*万|1,000,000/)
  }, 10_000)
})
