import { fireEvent, render, screen } from '@testing-library/react'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'

import { mockApi, enableImmediateMockWaitForTests, resetMockWaitForTests } from '../shared/api/mock'
import type { StrategyDraft } from '../shared/api/types'
import { ParamsScreen } from './index'

// Component wiring only: no model or market-data acceptance is claimed here.
let draft: StrategyDraft
beforeAll(async () => {
  vi.stubEnv('VITE_DATA_AS_OF_DATE', '2026-09-05')
  enableImmediateMockWaitForTests()
  const outcome = await mockApi.compile({
    utterance: '东方财富创20日新高且放量1.5倍买入，跌破20日线卖出',
    instrument: { symbol: '300059.SZ', name: '东方财富', market: 'CN_A', exchange: 'SZSE' },
  })
  if (outcome.status !== 'compiled') throw new Error('Expected a component fixture')
  draft = { ...outcome.draft, backtest: { ...outcome.draft.backtest, start: '2025-09-05', end: '2026-09-05' } }
})
afterAll(() => { vi.unstubAllEnvs(); resetMockWaitForTests() })

const renderSettings = () => {
  const onChange = vi.fn()
  const onBack = vi.fn()
  const onReset = vi.fn()
  render(<ParamsScreen open draft={draft} onChange={onChange} onBack={onBack}
    onReset={onReset} isLocked={false} focus="range" />)
  return { onChange, onBack, onReset }
}

describe('backtest date editing', () => {
  it('validates and commits the native date value even when no change event fires', () => {
    const { onChange, onBack } = renderSettings()
    const start = screen.getByLabelText<HTMLInputElement>('开始日期')
    // Reproduce a native date control whose DOM value changes without React notification.
    start.value = '0686-09-05'
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '完成' }))
    expect(onBack).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
    expect(start).toHaveValue('0686-09-05')
    expect(screen.getByRole('alert')).toHaveTextContent('不能早于 1990-01-01')

    start.value = '2010-09-05'
    fireEvent.click(screen.getByRole('button', { name: '完成' }))
    expect(onBack).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledExactlyOnceWith({
      ...draft, backtest: { ...draft.backtest, start: '2010-09-05' },
    })
  })

  it('keeps empty and unreasonable years in the editor and blocks every exit until corrected', () => {
    const { onChange, onBack } = renderSettings()
    const start = screen.getByLabelText('开始日期')
    expect(start).toHaveAttribute('min', '1990-01-01')
    expect(start).toHaveAttribute('max', '2026-09-05')
    fireEvent.change(start, { target: { value: '' } })
    expect(screen.getByRole('alert')).toHaveTextContent('请填写完整的开始日期')
    fireEvent.blur(start)
    expect(onChange).not.toHaveBeenCalled()

    fireEvent.change(start, { target: { value: '0686-09-05' } })
    fireEvent.click(screen.getByRole('button', { name: '完成' }))
    fireEvent.click(screen.getByRole('button', { name: '返回' }))
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onBack).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
    expect(start).toHaveValue('0686-09-05')
    expect(start).toHaveAttribute('aria-invalid', 'true')
    expect(start).toHaveFocus()
    expect(screen.getByRole('alert')).toHaveTextContent('不能早于 1990-01-01')

    fireEvent.change(start, { target: { value: '2010-09-05' } })
    fireEvent.blur(start)
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '完成' }))
    expect(onBack).toHaveBeenCalledTimes(1)
    expect(onChange).toHaveBeenCalledExactlyOnceWith({
      ...draft, backtest: { ...draft.backtest, start: '2010-09-05' },
    })
  })

  it('validates both buffered dates together and resets only on the explicit reset action', () => {
    const { onChange, onBack, onReset } = renderSettings()
    const start = screen.getByLabelText('开始日期')
    const end = screen.getByLabelText('结束日期')
    fireEvent.change(end, { target: { value: '2026-09-06' } })
    fireEvent.click(screen.getByRole('button', { name: '完成' }))
    expect(screen.getByRole('alert')).toHaveTextContent('结束日期不能晚于 2026-09-05')
    expect(onBack).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '重置为识别结果' }))
    expect(onReset).toHaveBeenCalledTimes(1)
    expect(end).toHaveValue(draft.backtest.end)
    fireEvent.change(end, { target: { value: '2020-09-05' } })
    expect(screen.getByRole('alert')).toHaveTextContent('开始日期不能晚于结束日期')
    fireEvent.change(start, { target: { value: '2010-09-05' } })
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onChange).toHaveBeenCalledExactlyOnceWith({
      ...draft, backtest: { ...draft.backtest, start: '2010-09-05', end: '2020-09-05' },
    })
    expect(onBack).toHaveBeenCalledTimes(1)
  })
})
