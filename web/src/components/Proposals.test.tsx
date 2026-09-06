import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { vi } from 'vitest'

import { Proposals, type ProposalItem } from './Proposals'

it('shows at most three complete paired cards and selects from anywhere in a card', async () => {
  const items: ProposalItem[] = ['东方财富', '贵州茅台', '比亚迪', '平安银行'].map((name, index) => ({
    id: `pair-${index}`,
    title: `${name} · 方案${index + 1}`,
    paired: true,
    detail: `组合${index + 1}的匹配理由。`,
    entry: '收盘价创20日新高',
    exit: '跌破20日均线',
  }))
  const onPick = vi.fn()
  const user = userEvent.setup()
  render(<Proposals items={items} onPick={onPick} />)

  const group = screen.getByRole('group', { name: '股票与策略组合' })
  const buttons = within(group).getAllByRole('button')
  const second = within(group).getByRole('button', { name: '贵州茅台 · 方案2' })
  expect(buttons).toHaveLength(3)
  expect(second).toHaveClass('proposal')
  expect(second).toHaveTextContent('贵州茅台 · 方案2')
  expect(screen.queryByRole('button', { name: '平安银行 · 方案4' })).not.toBeInTheDocument()
  expect(group.querySelector('details')).toBeNull()
  expect(screen.queryByText('组合2的匹配理由。')).not.toBeInTheDocument()
  expect(second).not.toHaveAttribute('aria-description', expect.stringContaining('匹配理由'))
  expect(within(second).getByText('收盘价创20日新高')).toBeVisible()
  expect(within(second).getByText('跌破20日均线')).toBeVisible()
  expect(onPick).not.toHaveBeenCalled()
  await user.click(within(second).getByText('收盘价创20日新高'))
  expect(onPick).toHaveBeenCalledExactlyOnceWith('pair-1')
})

it('keeps the original unpaired strategy card and selection behavior', async () => {
  const onPick = vi.fn()
  const user = userEvent.setup()
  const { container } = render(<Proposals items={[{
    id: 'legacy', title: '趋势确认', instrument: '东方财富 · 300059.SZ',
    entry: '上穿20日均线', exit: '下穿20日均线',
  }]} onPick={onPick} />)

  const choice = screen.getByRole('button', { name: '趋势确认' })
  expect(choice).toHaveClass('proposal')
  expect(choice).toHaveTextContent('东方财富 · 300059.SZ')
  expect(choice).toHaveTextContent('买入上穿20日均线')
  expect(choice).toHaveTextContent('卖出下穿20日均线')
  expect(container.querySelector('details')).toBeNull()
  await user.click(choice)
  expect(onPick).toHaveBeenCalledExactlyOnceWith('legacy')
})
