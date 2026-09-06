import { describe, expect, it } from 'vitest'

import { validateBacktestDates } from './backtest-date-validation'

describe('backtest date validation', () => {
  it.each([
    ['', '2026-09-05', 'start', '请填写完整的开始日期'],
    ['2025-09-05', '', 'end', '请填写完整的结束日期'],
    ['0686-09-05', '2026-09-05', 'start', '不能早于 1990-01-01'],
    ['2025-02-29', '2026-09-05', 'start', '有效的开始日期'],
    ['2025-09-05', '2026-09-06', 'end', '不能晚于 2026-09-05'],
    ['2026-09-05', '2025-09-05', 'start', '开始日期不能晚于结束日期'],
  ])('rejects an invalid range %s..%s', (start, end, field, reason) => {
    expect(validateBacktestDates(start, end, '2026-09-05')).toEqual({
      valid: false, field, reason: expect.stringContaining(reason),
    })
  })

  it.each(['1990-01-01', '2010-09-05', '2024-02-29'])('accepts %s without limiting the span', (start) => {
    expect(validateBacktestDates(start, '2026-09-05', '2026-09-05')).toEqual({ valid: true })
  })
})
