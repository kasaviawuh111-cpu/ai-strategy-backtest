import type { StrategyCondition } from './api/types'

const numberText = (value: number) => Number.isInteger(value) ? String(value) : String(Number(value.toFixed(6)))

function conditionText(condition: StrategyCondition): string {
  if (condition.kind === 'holding_period') return `持有 ${condition.sessions} 个交易日`
  if (condition.kind === 'position_return') return `${condition.exitTrigger === 'take_profit' ? '盈利' : '亏损'}达到 ${numberText(condition.thresholdPct)}%`
  if (condition.kind === 'trailing_drawdown') return `从持仓高点回落 ${numberText(condition.thresholdPct)}%`
  if (condition.kind === 'minute_protection') return condition.label.replace(/^分钟保护：/, '')
  if (condition.kind === 'indicator' && condition.indicatorId === 'technical.ma') {
    const period = condition.parameters.find(parameter => parameter.key === 'period')?.value
    if (typeof period === 'number' && (condition.label.includes('跌破') || condition.label.includes('下穿'))) return `跌破 ${period} 日线`
  }
  return condition.label.replace(/\s+/g, ' ').trim()
}

export function plainStrategyTitle(entry: StrategyCondition[], exit: StrategyCondition[]): string {
  const entryIndicators = new Set(entry.flatMap(condition => condition.kind === 'indicator' ? [condition.indicatorId] : []))
  const isTrendPullback = entry.length === 3
    && ['technical.ma_cross', 'price.return_pct', 'technical.ma'].every(id => entryIndicators.has(id))
  const buy = isTrendPullback
    ? '上涨趋势中回踩买入'
    : `${entry.map(conditionText).join('且')}时买入`
  if (!exit.length) return buy
  return `${buy}，${exit.map(conditionText).join('或')}卖出`
}
