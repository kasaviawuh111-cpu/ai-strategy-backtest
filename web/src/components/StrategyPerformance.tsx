import { MiniEquity } from './MiniEquity'
import { fmtPct, signClass } from './primitives'
import { sampleFrequency, type GallerySample } from '../shared/strategy-gallery-samples'

export function StrategyPerformance({ sample, loading = false }: { sample?: GallerySample; loading?: boolean }) {
  const snapshot = sample?.snapshot
  if (!snapshot || sample?.status !== 'ready') return <div className="sg-no-result" role="status">
    <p>{loading ? '正在读取样例回测' : sample?.executionAvailable === false ? '暂不可回测' : '样例回测暂不可用'}</p>
    <span>{loading ? '只读取已生成结果，不会自动发起回测。' : sample?.message || '尚无完整结果，可先查看策略规则。'}</span>
  </div>
  const { metrics } = snapshot
  const plan = snapshot.draft.strategySpec.trading_plan
  const rules = plan?.kind === 'conditional' && Array.isArray(plan.parameters.rules) ? plan.parameters.rules : []
  const orderShares = plan?.kind === 'grid' ? plan.parameters.order_shares
    : rules[0]?.quantity
  const capital = snapshot.draft.backtest.initialCashCny
  const openingShares = plan && plan.kind !== 'scheduled' ? Number(plan.parameters.opening_shares ?? 0) : 0
  const capitalTitle = openingShares > 0 && plan?.parameters.initial_capital_scope === 'total_equity' ? '初始总权益' : '初始资金'
  const capitalLabel = capital >= 10_000 ? `${(capital / 10_000).toLocaleString('zh-CN')}万元` : `${capital.toLocaleString('zh-CN')}元`
  const tradeLabel = metrics.tradeCountSemantics === 'closed_position_cycles' ? '全部卖出次数' : '完整交易'
  const completedLabel = metrics.tradeCountSemantics === 'closed_position_cycles' ? '全部卖出' : '完整交易'
  const comparable = metrics.benchmarkComparisonStatus === 'comparable'
  const points = comparable ? snapshot.series : snapshot.series.map(point => ({ ...point, benchmark: null }))
  return <div className="sg-performance">
    <div className="sg-return"><div><strong className={signClass(metrics.total)}>{fmtPct(metrics.total)}</strong><span>回测收益率</span></div>
      <span className="sg-result-label">样例 · {sampleFrequency(sample)}</span></div>
    <p className="sg-position-context">{capitalTitle}{capitalLabel} · {orderShares != null ? `每笔${orderShares}股` : `目标仓位${snapshot.draft.execution.allocationRatio * 100}%`}
      {openingShares > 0 && <> · 含期初持仓{openingShares}股</>}</p>
    <dl className="sg-metrics">
      <div><dt>最大回撤</dt><dd>{fmtPct(metrics.mdd)}</dd></div>
      <div><dt>胜率</dt><dd>{metrics.win == null ? '—' : `${metrics.win.toFixed(1)}%`}</dd></div>
      <div><dt>{tradeLabel}</dt><dd>{metrics.trips}<small> 次</small></dd></div>
    </dl>
    <MiniEquity series={points} marks={[]} label={`${snapshot.instrument.name}样例回测的收益走势${comparable ? '及买入持有对照' : ''}`} />
    <div className="sg-sample-range"><time>{metrics.dataRange.start}</time><span>至</span><time>{metrics.dataRange.end}</time></div>
    {metrics.trips < 5 && <p className="sg-sample-caution">{metrics.trips === 0
      ? metrics.tradeCountSemantics === 'closed_position_cycles' ? '尚未发生全部卖出，不代表没有成交；胜率暂未提供。' : '尚无完整交易，胜率暂无统计依据。'
      : metrics.win == null ? `仅${metrics.trips}次${completedLabel}，胜率暂未提供。` : `仅${metrics.trips}次${completedLabel}，胜率样本较少。`}</p>}
  </div>
}
