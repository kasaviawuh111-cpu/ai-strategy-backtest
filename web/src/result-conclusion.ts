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
    if (metrics.executionNote) return `${strategy}，本次没有已成交买入，无法计算收益率差。`
    return conclude(
      `${strategy}，本次没有已成交买入，无法计算收益率差${metrics.bench == null ? '' : `（${benchmark}，仅作参考）`}`,
    ).replace('；尚未发生全部卖出', '')
  }
  if (metrics.benchmarkComparisonStatus === 'benchmark_entry_not_filled') {
    return conclude(`${strategy}，买入持有基准未按同一成交规则成交，无法计算收益率差`)
  }
  if (metrics.benchmarkComparisonStatus === 'benchmark_unavailable') {
    return conclude(`${strategy}，没有可审计的买入持有基准，无法计算收益率差`)
  }

  if (metrics.total == null || metrics.bench == null || metrics.excess == null) {
    return conclude(`${strategy}，${benchmark}，收益率差未记录`)
  }

  const comparison = metrics.excess === 0
    ? '收益率持平'
    : `收益率${metrics.excess > 0 ? '领先' : '落后'} ${percentMagnitude(metrics.excess)} 个百分点`
  return conclude(`${strategy}，${benchmark}，${comparison}`)
}
