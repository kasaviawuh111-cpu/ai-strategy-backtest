import type { BacktestMetrics } from './types'
import { percentMagnitude } from './shared/percent'

const absolutePercent = (value: number) => `${percentMagnitude(value)}%`

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
    `${body}${metrics.trips === 0 ? metrics.tradeCountSemantics === 'closed_position_cycles'
      ? metrics.benchmarkComparisonStatus === 'comparable'
        ? '；已有买入，尚未全部卖出' : '；尚未发生全部卖出'
      : '；完整买卖 0 回合' : ''}。`

  if (metrics.benchmarkComparisonStatus === 'strategy_entry_not_filled') {
    if (metrics.executionNote) return `${strategy}，本次没有已成交买入，无法比较超额收益。`
    return conclude(
      `${strategy}，本次没有已成交买入，无法比较超额收益${metrics.bench == null ? '' : `（${benchmark}，仅作参考）`}`,
    ).replace('；尚未发生全部卖出', '')
  }
  if (metrics.benchmarkComparisonStatus === 'benchmark_entry_not_filled') {
    return conclude(`${strategy}，买入持有基准未按同一成交规则成交，无法比较超额收益`)
  }
  if (metrics.benchmarkComparisonStatus === 'benchmark_unavailable') {
    return conclude(`${strategy}，没有可审计的买入持有基准，无法比较超额收益`)
  }

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
    `${strategy}，${benchmark}，复合相对${comparison.replace('相对', '')} ${absolutePercent(metrics.excess)}`,
  )
}
