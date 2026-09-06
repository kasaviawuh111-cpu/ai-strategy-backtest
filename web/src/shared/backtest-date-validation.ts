import { dataAsOfDate } from './api/contract'

export const MINIMUM_BACKTEST_DATE = '1990-01-01'

type DateValidation =
  | { valid: true; reason?: undefined; field?: undefined }
  | { valid: false; reason: string; field: 'start' | 'end' }

const isCalendarDate = (value: string) => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const parsed = new Date(`${value}T00:00:00Z`)
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value
}

export const validateBacktestDates = (
  start: string,
  end: string,
  latestDate = dataAsOfDate(),
): DateValidation => {
  for (const [field, value, label] of [
    ['start', start, '开始日期'], ['end', end, '结束日期'],
  ] as const) {
    if (!value) return { valid: false, field, reason: `请填写完整的${label}。` }
    if (!isCalendarDate(value)) {
      return { valid: false, field, reason: `请填写有效的${label}（年、月、日）。` }
    }
    if (value < MINIMUM_BACKTEST_DATE) {
      return { valid: false, field, reason: `${label}不能早于 1990-01-01，请检查年份。` }
    }
    if (value > latestDate) {
      return { valid: false, field, reason: `${label}不能晚于 ${latestDate}，请检查日期。` }
    }
  }
  if (start > end) {
    return { valid: false, field: 'start', reason: '回测开始日期不能晚于结束日期。' }
  }
  return { valid: true }
}
