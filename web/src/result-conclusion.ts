import type { BacktestMetrics } from './types'

const absolutePercent = (value: number) => `${Math.abs(value).toFixed(2)}%`

const performancePhrase = (label: string, value: number | null) => {
  if (value == null) return `${label}收益未记录`
  if (value > 0) return `${label}盈利 ${absolutePercent(value)}`
  if (value < 0) return `${label}亏损 ${absolutePercent(value)}`
  return `${label}收益 0.00%`
}

/**
 * 结果一句话。
 * 口径不变（仍是精确数字 + 相对关系），只把基准从「同资金买入持有」换成
 * 「同样的钱买入后一直持有」——同一个概念，不需要先懂术语才能读懂这句话。
 */
export function numericResultConclusion(metrics: BacktestMetrics) {
  const strategy = performancePhrase('策略', metrics.total)
  const benchmark = performancePhrase('同样的钱买入后一直持有', metrics.bench)
  const conclude = (body: string) =>
    `${body}${metrics.trips === 0 ? '；完整买卖 0 回合' : ''}。`

  if (metrics.total == null || metrics.bench == null || metrics.excess == null) {
    return conclude(`${strategy}，${benchmark}，相对收益未记录`)
  }

  let comparison: string
  if (metrics.excess === 0) {
    comparison = '相对持平'
  } else if (metrics.total < 0 && metrics.bench < 0) {
    comparison = metrics.excess > 0 ? '相对少亏' : '相对多亏'
  } else {
    comparison = metrics.excess > 0 ? '相对领先' : '相对落后'
  }

  return conclude(
    `${strategy}，${benchmark}，${comparison} ${Math.abs(metrics.excess).toFixed(2)} 个百分点`,
  )
}
