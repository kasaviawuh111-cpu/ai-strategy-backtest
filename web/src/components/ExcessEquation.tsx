import { fmtPct, signClass } from './primitives'

export const BENCHMARK_LABEL = '买入后一直持有'

/** All values are in percentage points; comparison requires an audited benchmark. */
export function ExcessEquation({ total, bench, excess, comparisonStatus = 'comparable', compact = false }: {
  total: number | null;
  bench: number | null;
  excess: number | null;
  comparisonStatus?: 'comparable' | 'strategy_entry_not_filled' | 'benchmark_entry_not_filled' | 'benchmark_unavailable';
  compact?: boolean;
}) {
  if (comparisonStatus !== 'comparable' || total == null || bench == null || excess == null) {
    const reason = comparisonStatus === 'strategy_entry_not_filled'
      ? '策略没有已成交买入，无法计算收益率差'
      : comparisonStatus === 'benchmark_entry_not_filled'
        ? '买入持有基准未成交，无法计算收益率差'
        : '本次没有可审计的可比基准'
    return <div className="equation-unavailable"><span>交易策略超额 —</span><small>{reason}</small></div>
  }
  return (
    <div className={`equation${compact ? ' equation--compact' : ''}`} role="group"
      aria-label={`策略收益率 ${fmtPct(total)} 减去${BENCHMARK_LABEL}收益率 ${fmtPct(bench)}，交易策略超额 ${fmtPct(excess).replace('%', '')} 个百分点`}>
      <div className="eq-piece">
        <b className={signClass(total)}>{fmtPct(total)}</b><span>策略收益率</span>
        <i className="eq-op" aria-hidden="true">−</i>
      </div>
      <div className="eq-piece">
        <b className={signClass(bench)}>{fmtPct(bench)}</b><span>{BENCHMARK_LABEL}收益率</span>
        <i className="eq-op" aria-hidden="true">=</i>
      </div>
      <div className={`eq-piece eq-result ${signClass(excess)}`}>
        <b>{fmtPct(excess).replace('%', '')}<small>个百分点</small></b><span>交易策略超额</span>
      </div>
    </div>
  )
}
