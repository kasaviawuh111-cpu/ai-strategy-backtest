import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { beforeAll, afterAll, expect, it, vi } from 'vitest'
import { RuleSpecEditor } from './RuleSpecEditor'
import { StrategyCard } from './SummaryCards'
import { toStrategySummary } from '../view-model'
import { toLiveRevisionBody, withEditedStrategySpec } from '../shared/api/contract'
import { mockApi, enableImmediateMockWaitForTests, resetMockWaitForTests } from '../shared/api/mock'
import type { StrategyDraft } from '../shared/api/types'

let base: StrategyDraft
beforeAll(async () => {
  enableImmediateMockWaitForTests()
  const result = await mockApi.compile({ utterance: '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出',
    instrument: { name: '东方财富', symbol: '300059.SZ', market: 'CN_A', exchange: 'SZSE' } })
  if (result.status !== 'compiled') throw new Error('fixture unavailable')
  base = result.draft
})
afterAll(resetMockWaitForTests)

it('persists edited logical relation into the revision payload', () => {
  const capture = vi.fn()
  function Harness() {
    const [draft, setDraft] = useState(base)
    return <RuleSpecEditor draft={draft} side="entry" onChange={(next) => { setDraft(next); capture(toLiveRevisionBody(next)) }} />
  }
  render(<Harness />)
  fireEvent.change(screen.getByRole('combobox', { name: 'entry条件关系' }), { target: { value: 'any' } })
  expect(capture.mock.calls.at(-1)?.[0].strategy.entry.type).toBe('any')
})

it('edits holding sessions without changing entry, fees or independent exit semantics', () => {
  const draft = withEditedStrategySpec(base, { ...base.strategySpec, exit: { op: 'first_of', children: [{
    type: 'holding_period_exit', sessions: 10, anchor: 'first_entry_fill', count_mode: 'subsequent_trading_sessions', execution: 'target_session_open_proxy',
  }] } })
  const change = vi.fn()
  render(<RuleSpecEditor draft={draft} side="exit" path="exit-0" onChange={change} />)
  expect(screen.getByRole('combobox', { name: '卖出条件关系' })).toBeDisabled()
  fireEvent.change(screen.getByRole('spinbutton', { name: '持有交易日' }), { target: { value: '20' } })
  const next = change.mock.calls[0][0]
  expect(toLiveRevisionBody(next).strategy.exit.children[0].sessions).toBe(20)
  expect(next.execution).toEqual(draft.execution)
  expect(next.strategySpec.entry).toEqual(draft.strategySpec.entry)
})

it('shows separate condition buttons, relation and unboxed range with correct edit target', () => {
  const edit = vi.fn()
  const { container } = render(<StrategyCard instrument={{ name: '东方财富', code: '300059.SZ' }} strategy={toStrategySummary(base)}
    onEditRow={edit} onOpenMore={vi.fn()} onRun={vi.fn()} />)
  expect(screen.getByRole('button', { name: '全部满足才买入（且）' })).toBeInTheDocument()
  fireEvent.click(container.querySelector('.condition-section .erow')!)
  expect(edit).toHaveBeenCalledWith('entry', 'entry-0')
  expect(container.querySelector('.condition-range')).not.toHaveClass('erow')
})
