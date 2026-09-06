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
  const next = change.mock.calls[0]?.[0]
  expect(toLiveRevisionBody(next).strategy.exit.children[0]).toMatchObject({ sessions: 20 })
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

it('persists ALL across indicator and holding exits and labels the real relation', () => {
  const initial = withEditedStrategySpec(base, { ...base.strategySpec, exit: { op: 'first_of', children: [
    base.strategySpec.exit.children[0]!, { type: 'holding_period_exit', sessions: 10, anchor: 'first_entry_fill',
      count_mode: 'subsequent_trading_sessions', execution: 'target_session_open_proxy' },
  ] } })
  const change = vi.fn()
  render(<RuleSpecEditor draft={initial} side="exit" onChange={change} />)
  fireEvent.change(screen.getByRole('combobox', { name: '卖出条件关系' }), { target: { value: 'all' } })
  const next = change.mock.calls[0]?.[0]
  expect(toLiveRevisionBody(next).strategy.exit.op).toBe('all')
  expect(next.exit.conditions[1].label).toBe('持有满 10 个交易日')
  expect(toStrategySummary(next).exitRule).toMatchObject({ operator: 'all' })
})

it('edits verified financial and event values in the executable payload', () => {
  const initial = withEditedStrategySpec(base, { ...base.strategySpec, entry: {
    type: 'financial_condition', metric_id: 'financial.test', definition_version: '1.0.0', report_type: 'annual',
    period_basis: 'full_year', statement_scope: 'consolidated', revision_policy: 'as_known_at_signal', comparator: 'gt', value: 10, unit: 'PERCENT',
  } })
  const change = vi.fn()
  const { unmount } = render(<RuleSpecEditor draft={initial} side="entry" onChange={change} />)
  fireEvent.change(screen.getByRole('spinbutton', { name: '财务阈值（PERCENT）' }), { target: { value: '15' } })
  expect(toLiveRevisionBody(change.mock.calls[0]?.[0]).strategy.entry).toMatchObject({ value: 15, revision_policy: 'as_known_at_signal' })
  unmount()
  const event = withEditedStrategySpec(base, { ...base.strategySpec, entry: {
    type: 'event_condition', event_code: 'event.test', definition_version: '1.0.0', trigger: 'published', attributes: { count: 2 },
  } })
  render(<RuleSpecEditor draft={event} side="entry" onChange={change} />)
  fireEvent.change(screen.getByRole('spinbutton', { name: '事件参数 count' }), { target: { value: '3' } })
  expect(toLiveRevisionBody(change.mock.calls.at(-1)?.[0]).strategy.entry).toMatchObject({ attributes: { count: 3 } })
})
