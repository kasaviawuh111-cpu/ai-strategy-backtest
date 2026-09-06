import { fireEvent, render, screen } from '@testing-library/react'
import { afterAll, beforeAll, expect, it, vi } from 'vitest'
import { mockApi, enableImmediateMockWaitForTests, resetMockWaitForTests } from '../shared/api/mock'
import type { StrategyDraft } from '../shared/api/types'
import { StrategySlotComposer } from './StrategySlotComposer'

let draft: StrategyDraft
beforeAll(async () => {
  enableImmediateMockWaitForTests()
  const result = await mockApi.compile({ utterance: '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出',
    instrument: { symbol: '300059.SZ', name: '东方财富', market: 'CN_A', exchange: 'SZSE' } })
  if (result.status !== 'compiled') throw new Error('Missing fixture')
  draft = result.draft
})
afterAll(resetMockWaitForTests)

it('selects on entry but preserves a partial selection on subsequent clicks and edits', () => {
  const submit = vi.fn()
  render(<StrategySlotComposer draft={draft} mode="rules" disabled={false} onSubmit={submit} onClear={vi.fn()} />)
  const entry = screen.getByLabelText('买入') as HTMLTextAreaElement
  const stock = screen.getByLabelText('股票') as HTMLTextAreaElement
  expect(entry.selectionEnd).toBe(entry.value.length)
  fireEvent.pointerDown(stock)
  stock.focus()
  fireEvent.click(stock)
  expect(stock.selectionStart).toBe(0)
  expect(stock.selectionEnd).toBe(stock.value.length)
  fireEvent.pointerDown(stock)
  stock.setSelectionRange(1, 3)
  fireEvent.click(stock)
  expect(stock.selectionStart).toBe(1)
  expect(stock.selectionEnd).toBe(3)
  fireEvent.change(stock, { target: { value: '贵州茅台' } })
  fireEvent.change(entry, { target: { value: entry.value.replace('20', '30') } })
  fireEvent.click(screen.getByRole('button', { name: '识别交易规则' }))
  expect(submit).toHaveBeenCalledOnce()
  expect(submit.mock.calls[0][0]).toContain('贵州茅台')
  expect(submit.mock.calls[0][0]).not.toContain('卖出条件改为')
})

it('supports replacing all slots and blocks submission while a slot is empty', () => {
  const submit = vi.fn()
  render(<StrategySlotComposer draft={draft} mode="stock" disabled={false} onSubmit={submit} onClear={vi.fn()} />)
  fireEvent.change(screen.getByLabelText('股票'), { target: { value: '' } })
  expect(screen.getByRole('button', { name: '识别交易规则' })).toBeDisabled()
  for (const [label, value] of [['股票', '美的集团'], ['买入', 'RSI上穿30'], ['卖出', 'RSI高于55']]) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } })
  }
  fireEvent.click(screen.getByRole('button', { name: '识别交易规则' }))
  expect(submit.mock.calls[0][0]).toContain('买入条件改为：RSI上穿30。卖出条件改为：RSI高于55')
})
