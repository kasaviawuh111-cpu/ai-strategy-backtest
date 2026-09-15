import { afterEach, describe, expect, it } from 'vitest'
import { dataAsOfDate, setAvailableDataEnd } from './api/contract'

import { validateBacktestDates } from './backtest-date-validation'

describe('backtest date validation', () => {
  afterEach(() => setAvailableDataEnd(null))
  it('keeps the imported watermark across Monday and advances on the next import', () => {
    setAvailableDataEnd('2026-09-11')
    expect(dataAsOfDate(new Date('2026-09-14T06:00:00Z'))).toBe('2026-09-11')
    expect(validateBacktestDates('2025-09-11', '2026-09-11')).toEqual({ valid: true })
    expect(validateBacktestDates('2025-09-11', '2026-09-14').valid).toBe(false)
    setAvailableDataEnd('2026-09-18')
    expect(dataAsOfDate()).toBe('2026-09-18')
  })
  it.each([
    ['', '2026-09-05', 'start', '请填写完整的开始日期'],
    ['2025-09-05', '', 'end', '请填写完整的结束日期'],
    ['0686-09-05', '2026-09-05', 'start', '不能早于 1990-01-01'],
    ['2025-02-29', '2026-09-05', 'start', '有效的开始日期'],
    ['2025-09-05', '2026-09-06', 'end', '数据尚未更新'],
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
