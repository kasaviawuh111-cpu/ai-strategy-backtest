/** Display percentage points without rounding a nonzero result to zero. */
export function percentMagnitude(value: number): string {
  const magnitude = Math.abs(value)
  return magnitude > 0 && magnitude < 0.005
    ? Number(magnitude.toPrecision(2)).toString()
    : magnitude.toFixed(2)
}

export function formatPercent(value: number | null): string {
  if (value == null || !Number.isFinite(value)) return '—'
  return `${value > 0 ? '+' : value < 0 ? '-' : ''}${percentMagnitude(value)}%`
}
