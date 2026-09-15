import { expect, it } from 'vitest'
import { formatPercent, percentMagnitude } from './percent'

it('keeps small nonzero returns and their signs visible without changing normal precision', () => {
  expect(formatPercent(-0.000844)).toBe('-0.00084%')
  expect(formatPercent(0.000844)).toBe('+0.00084%')
  expect(percentMagnitude(-0.000844)).toBe('0.00084')
  expect(formatPercent(0)).toBe('0.00%')
  expect(formatPercent(-0)).toBe('0.00%')
  expect(formatPercent(-1.234)).toBe('-1.23%')
  expect(formatPercent(null)).toBe('—')
  expect(formatPercent(Number.NaN)).toBe('—')
})
